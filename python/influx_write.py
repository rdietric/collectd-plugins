# coding=utf-8

"""
Send metrics to InfluxDB (https://github.com/influxdb/influxdb/) using the
InfluxDBClient interface.

Collectd Values are sent/mapped to InfluxDB as follows:
measurement <- plugin
field/metric name <- type instance, if available, otherwise type
tag name/type (metric specific) <- either the plugin name or 'cpu', if the 
                                   plugin ends with 'cpu' or '_socket'
tag value (metric specific) <- plugin instance

Additionally, the host name is written as tag for 'hostname'.

A collectd value is identified by plugin, plugin instance, type and type instance.
"""

import collectd
import os
import math
import threading
import re

try:
    from influxdb.client import InfluxDBClient
except ImportError:
    InfluxDBClient = None

influx = None

ssl = False
hostname = 'localhost'
port = 8086
username = None
password = None
database = None # name of the database

batch_count = 0
batch_size = 200   # number of metrics to be sent in one batch
cache_size = 2000  # maximum number of metrics to store locally (e.g. if sends fail)
batch = {} # all unsent value lists are stored here

store_rates = False
batch_derive = {} # all previous value lists of derived/counter types are stored here

debug = True
time_precision = 's'

"""
Set plugin configuration (from collectd config file).
"""
def set_config(config):
  if config.values[0] == 'influx_write':
    collectd.info("[InfluxDB Writer] Get configuration")
    for value in config.children:
      if value.key == 'ssl':
        global ssl
        ssl = bool(value.values[0])
      elif value.key == 'host':
        global hostname
        hostname = value.values[0]
      elif value.key == 'port':
        global port
        port = int(value.values[0])
      elif value.key == 'user':
        global username
        username = value.values[0]
      elif value.key == 'pwd':
        global password
        password = value.values[0]
      elif value.key == 'database':
        global database
        database = value.values[0]
      elif value.key == 'batch_size': 
        global batch_size
        batch_size = _getInteger(value.values[0])
      elif value.key == 'cache_size':
        global cache_size
        cache_size = _getInteger(value.values[0])
      elif value.key == 'StoreRates':
        global store_rates
        store_rates = value.values[0]
        if store_rates:
          collectd.info("[InfluxDB Writer] Store rates for derived and counter types")
      else:
        collectd.info("[InfluxDB Writer] Ignore unknown option %s" % (value.key,))

"""
Connect to the InfluxDB server
"""
def _connect():
  try:
      # Open Connection
      global influx
      influx = InfluxDBClient(host=hostname, port=port, username=username, 
                              password=password, database=database, ssl=ssl)
      
      collectd.info("[InfluxDB Writer] Established connection to %s:%d/%s." % (hostname, port, database) )
  except Exception as ex:
      # Log Error
      collectd.info("[InfluxDB Writer] Failed to connect to %s:%s/%s. (%s:%s) - %s" % (hostname, port, database, username, password, ex) )
      _close()

"""
Close the socket = do nothing for influx which is http stateless
"""
def _close():
    global influx
    influx = None

"""
Collectd initialization callback.
Responsible for starting the sending thread
"""
def init_callback():
  global InfluxDBClient
  if not InfluxDBClient:
    collectd.info('[InfluxDB Writer] influxdb.client.InfluxDBClient import failed.')
  else:
    #collectd.info('[InfluxDB Writer] Initialize.')
    _connect()
  
def write(valueList, data=None):
  if not InfluxDBClient:
    return

  #collectd.info('[InfluxDB Writer] %s' % (str(valueList),))
  #if data:
  #  collectd.info('[InfluxDB Writer] Data: %s' % (str(data),))

  global batch
  global batch_count
  if batch_count <= cache_size:
    # Add the data to the batch
    #_store_per_plugin(valueList)
    _store_per_plugin_instance(valueList)
    batch_count += 1

  # If there are sufficient metrics, then pickle and send
  if batch_count >= batch_size:
    collectd.debug("[InfluxDB Writer] Sending batch size: %d/%d" % (batch_count, batch_size))
    _send()

"""
Store values per per plugin instance. 
Plugin name and plugin instance (tag) identify a value list. 
"""
def _store_per_plugin_instance(valueList):
  global batch

  if valueList.plugin:
    plugin_name = valueList.plugin
  elif valueList.type:
    plugin_name = valueList.type
  else:
    collectd.error('[InfluxDB Writer] Either a plugin or type is required!')
    return

  tag = valueList.plugin_instance

  #collectd.info("Write valueList: %s" % (valueList,))

  # create array for plugin and tag, if it is not available yet
  if plugin_name in batch:
    if tag in batch[plugin_name]:
      # append value
      batch[plugin_name][tag].append(valueList)
    else:
      # create array of values for new tag
      batch[plugin_name][tag] = [valueList]
  else:
    # add the plugin and the tag with a new value
    batch[plugin_name] = {tag:[valueList]}

"""
Send data to InfluxDB. Data that cannot be sent will be kept in cache.
"""
def _send():
  global batch
  global batch_count

  if not influx:
    collectd.info('[InfluxDB Writer] Connection not available. Try reconnect ...')
    _connect()

  metrics = _prepare_metrics()

  # reset batch which only contains initial values of derived metrics
  if len(metrics) == 0:
    batch = {}
    batch_count = 0
    if len(batch_derive) == 0:
      collectd.info('[InfluxDB Writer] No metrics to send. '
        'No previous values are stored. Should not happen!')
    return

  # Send data to InfluxDB (len(metrics) <= batch_count as NaN and inf are not moved from batch to metrics)
  #collectd.info('[InfluxDB Writer] Write %d series of data' % (len(metrics)))
  collectd.info('[InfluxDB Writer] Write %d series of data (%d rates)' % (len(metrics), len(batch_derive)))

  ret = False

  if influx:
    try:
      ret = influx.write_points(metrics, time_precision=time_precision)
    except Exception as ex:
      collectd.error("[InfluxDB Writer] Error sending metrics(%s)" % (ex))
      #raise

  # empty batch buffer for successful writes
  if ret:
    #collectd.info("reset batch")
    batch = {}
    batch_count = 0

def _prepare_metrics():
  global batch

  # build metrics data
  metrics = []
  for measurement in batch:
    for tag in batch[measurement]:
      last_time = -1
      fields = {}

      # iterate over the value lists
      for valueList in batch[measurement][tag]:
        counterMetricID = None # default is a gauge metric type, no metric ID needed

        time = int(valueList.time)

        # if the tag (plugin instance) is not None, add it with measurement (plugin) as key
        tags = {"hostname": valueList.host}
        if tag:
          if measurement.endswith('cpu') or measurement.endswith('_socket'):
            # plugin instance is CPU core
            tags['cpu'] = tag #_getInteger(tag)
          elif measurement == 'nvml' or measurement.startswith('gpu'):
            # plugin instance is GPU id
            tags['gpu'] = tag
          else:
            tags[measurement] = tag

        # determine metric name
        metricName = valueList.type_instance
        if metricName is None or metricName == '':
          metricName = valueList.type
          #metricName = "value"
        field_name = metricName
        
        if len(valueList.values) == 0:
          collectd.info("No values available for %s:%s!" % (measurement,metricName))
          continue

        for midx, value in enumerate(valueList.values):
          # ignore invalid values
          if str(value) == "nan" or math.isnan(float(value)) or str(value) == "inf":
            continue

          # get dataset to determine types
          ds = collectd.get_dataset(valueList.type)

          # get metric name from data type, if we have more than one value
          if len(valueList.values) > 1:
            try:
              # prepend type name
              field_name = ds[midx][0] + "_" + metricName
            except:
              field_name = metricName + str(midx)

          # for derived counters
          if store_rates and (ds[midx][1] == 'derive' or ds[midx][1] == 'counter'):
            #collectd.info("Derived: %s (%s)" % (valueList,ds))

            # determine metric identifier from mandatory and optional values
            if counterMetricID == None:
              counterMetricID = valueList.plugin+valueList.type # mandatory identifier
              # optional identifier
              if valueList.plugin_instance:
                counterMetricID += valueList.plugin_instance
              if valueList.type_instance:
                counterMetricID += valueList.type_instance

            #collectd.info(metric_key)
            if counterMetricID in batch_derive:
              prevValueList = batch_derive[counterMetricID]
              diff_time = time - int(prevValueList.time)
              if diff_time > 0:
                # determine the rate
                diff_value = value - prevValueList.values[midx]
                value = float(diff_value) / float(diff_time)

                #collectd.info("[InfluxDB Writer] %s: Rate: %f" % (measurement+"_"+tag+"_"+field_name, value) )
              else:
                # can occur, if we have the same plugin and plugin instance,
                # but different types (e.g. with the disk plugin)
                if prevValueList.type == valueList.type:
                  collectd.warning("[InfluxDB Writer] Found a previous value "
                    "for this metric with the same timestamp (prev: %s, curr: %s)"
                    % (batch_derive[counterMetricID], valueList) )
                continue
            else:
              continue

          # if possible, write all fields in a single line
          # if next value has the same timestamp, add it as another field
          # works only, if different fields/values are read within the same second
          if time == last_time:
            # add field values to measurement point
            fields[field_name] = value
          else:
            if fields: # fields are available, but time changed
              # write last fields with last timestamp              
              metrics.append({
                  "measurement": measurement,
                  "time": last_time,
                  "tags": tags,
                  "fields": fields}
                  )
              #collectd.info("Data point: %s:%d, tags: %s, fields: %s" % (measurement, int(last_time), str(tags), str(fields)))

            # write first field value for next measurement point
            fields = {field_name: value}

          # remember last timestamp
          last_time = time

        # store values to determine rates
        if counterMetricID:
          batch_derive[counterMetricID] = valueList

      # write remaining fields
      if fields:
        #collectd.info("Remaining data point: %s:%d, tags: %s, fields: %s" % (measurement, int(last_time), str(tags), str(fields)))
        metrics.append({
            "measurement": measurement,
            "time": last_time,
            "tags": tags,
            "fields": fields}
            )

  return metrics

pattern = re.compile(r'\d')
def _getInteger(stringInt):
  ret = None
  try:
    ret = int(stringInt)
  except ValueError as e:
    ret = pattern.match(stringInt)
    if ret:
      return int(ret.group())
    
  return ret
  
def flush(timeout, identifier):
  global batch_count
  collectd.info("[InfluxDB Writer] Flush metrics in batch with size %d." % (batch_count))

  # Send pickled batch
  _send()
    
collectd.register_config(set_config)
collectd.register_write(write)
collectd.register_init(init_callback)
collectd.register_flush(flush)
