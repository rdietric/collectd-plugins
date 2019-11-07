# coding=utf-8

"""
Send metrics to InfluxDB (https://github.com/influxdb/influxdb/) using the
InfluxDBClient interface.
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

hostname = 'localhost'
port = 8086
username = 'user'
password = 'pwd'
database = 'db'
divide_cpu_used_by_100 = False

batch_count = 0
batch_size = 200
cache_size = 2000
batch = {}

debug = True
time_precision = 's'

def set_config(config):
  if config.values[0] == 'influx_write':
    collectd.info("[InfluxDB Writer] Get configuration")
    for value in config.children:
      if value.key == 'host':
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


def _connect():
  """
  Connect to the InfluxDB server
  """
  try:
      # Open Connection
      global influx
      influx = InfluxDBClient(host=hostname, port=port, username=username, 
                              password=password, database=database)
      
      collectd.info("[InfluxDB Writer] Established connection to %s:%d/%s." % (hostname, port, database) )
  except Exception as ex:
      # Log Error
      collectd.info("[InfluxDB Writer] Failed to connect to %s:%s/%s. (%s:%s) - %s" % (hostname, port, database, username, password, ex) )
      _close()

def _close():
    """
    Close the socket = do nothing for influx which is http stateless
    """
    global influx
    influx = None

def init_callback():
  """
  Collectd initialization callback.
  Responsible for starting the sending thread
  """

  global InfluxDBClient
  if not InfluxDBClient:
    collectd.info('[InfluxDB Writer] influxdb.client.InfluxDBClient import failed.')
  else:
    collectd.info('[InfluxDB Writer] Initialize.')
    _connect()
  
def write(valueList, data=None):
  if not InfluxDBClient:
    return

  #print "thread:", threading.currentThread().ident

  collectd.info('[InfluxDB Writer] %s' % (str(valueList),))
  if data:
    collectd.info('[InfluxDB Writer] Data: %s' % (str(data),))

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
Store values per per plugin. This enables to send a point with multiple values.
"""
def _store_per_plugin(valueList):
  global batch

  if valueList.plugin:
    plugin_name = valueList.plugin
  else:
    plugin_name = valueList.type

  # create array for collector, if it is not available yet
  if plugin_name not in batch:
    batch[plugin_name] = []

  batch[plugin_name].append(valueList)

"""
Store values per per plugin[instance]. This enables to send a point with multiple values/fields.
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

  #tag = None
  # plugin instance is used as tag
  #if valueList.plugin_instance:
  tag = valueList.plugin_instance

  #if valueList.plugin.endswith('cpu') or valueList.plugin.endswith('_socket'): 
  #  tag = _getInteger(tag)

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
  if not influx:
    collectd.info('[InfluxDB Writer] Connection not available. Try reconnect ...')
    _connect()

  #metrics = _send_multiple_values_per_line()
  metrics = _send_new()

  if len(metrics) == 0:
    return

  # Send data to influxdb
  if debug:
    collectd.info('[InfluxDB Writer] Write %d series of data' % (len(metrics)))

  ret = False

  if influx:
    try:
      ret = influx.write_points(metrics, time_precision=time_precision)
    except Exception as ex:
      collectd.error("[InfluxDB Writer] Error sending metrics(%s)" % (ex))
      #raise

  # empty batch buffer for successful writes
  if ret:
    global batch
    global batch_count

    batch = {}
    batch_count = 0

def _send_new():
  global batch

  # build metrics data
  metrics = []
  for measurement in batch.keys():
    for tag in batch[measurement]:
      last_time = -1
      fields = {}

      # iterate over the value lists
      for valueList in batch[measurement][tag]:
        time = int(valueList.time)

        # if the tag (plugin instance) is not None, add it with measurement (plugin) as key
        tags = {"hostname": valueList.host}
        if tag:
          if measurement.endswith('cpu') or measurement.endswith('_socket'): 
            tags['cpu'] = tag #_getInteger(tag)
          else:
            tags[measurement] = tag

        # determine metric name
        metricName = valueList.type_instance
        if metricName is None or metricName == '':
          metricName = valueList.type
          #metricName = "value"
        field_name = metricName
        
        #collectd.info("Metric: %s" % (str(metric),))
        if len(valueList.values) == 0:
          collectd.info("No values available for %s:%s!" % (measurement,metricName))
          continue

        for midx, value in enumerate(valueList.values):
          # ignore invalid values
          if str(value) == "nan" and math.isnan(float(value)) and str(value) == "inf":
            continue

          # get metric name from data type, if we have more than one value
          if len(valueList.values) > 1:
            type_info = collectd.get_dataset(valueList.type) 
            #if type_info:
            #  print type_info
            try:
              # prepend value type
              field_name = type_info[midx][0] + "_" + metricName
            except:
              field_name = metricName + str(midx)

          # if possible, write all fields in a single line
          # if next value has the same timestamp, add it as another field
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
              collectd.info("Data point: %s:%d, tags: %s, fields: %s" % (measurement, int(last_time), str(tags), str(fields)))

            # write first field value for next measurement point
            fields = {field_name: value}

          # remember last timestamp
          last_time = time

      # write remaining fields
      if fields: 
        collectd.info("Remaining data point: %s:%d, tags: %s, fields: %s" % (measurement, int(last_time), str(tags), str(fields)))
        metrics.append({
            "measurement": measurement,
            "time": last_time,
            "tags": tags,
            "fields": fields}
            )

  return metrics

def _send_one_value_per_line():
  global batch

  # build metrics data
  metrics = []
  for measurement in batch.keys():
    for value_obj in batch[measurement]:
      time = int(value_obj.time)
      tags = {"hostname": value_obj.host}

      # the plugin field contains the measurement name
      if value_obj.plugin:
        mname = value_obj.plugin
      else:
        mname = value_obj.type

      # if plugin instance is given, interpret it as CPU number
      if value_obj.plugin_instance:
        tags['cpu'] = int(value_obj.plugin_instance)
      
      # the type instance contains the field/metric name
      if value_obj.type_instance:
        field_name = value_obj.type_instance
      else:
        field_name = 'value'

      for value in value_obj.values:
        if str(value) == "nan" or math.isnan(float(value)):
          value = 0

        #collectd.info("%s:%s %f at %s, host %s" % (value_obj.plugin, value_obj.type_instance, value, value_obj.time, value_obj.host))

        metrics.append({
          "measurement": mname,
          "time": time,
          "tags": tags,
          "fields": {field_name: value}}
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

"""
Wrap multiple values in a single send, if tags (time, cpu, hostname) do not change.
"""
def _send_multiple_values_per_line():
  global batch

  metrics = [] # new metrics array
  for measurement in batch.keys():
    last_valueList = None
    last_cpu = ""
    last_gpu = ""
    fields = {}
    tags = {}
    last_tags = {}
    for valueList in batch[measurement]:
      tags = {"hostname": valueList.host}
      time = valueList.time

      # determine metric name
      metricName = valueList.type_instance
      if metricName is None or metricName == '':
        metricName = "value"
      field_name = metricName

      # if there is a plugin_instance, we add it as tag with plugin name as tag name
      if valueList.plugin_instance and valueList.plugin:
        tags[valueList.plugin] = valueList.plugin_instance

      
      #collectd.info("Metric: %s" % (str(metric),))
      if len(valueList.values) == 0:
        collectd.info("No values available for %s:%s!" % (measurement,metricName))
        continue

      for midx, value in enumerate(valueList.values):
        # ignore invalid values
        if str(value) == "nan" and math.isnan(float(value)) and str(value) == "inf":
          continue

        # get metric name from data type, if we have more than one value
        if len(valueList.values) > 1:
          type_info = collectd.get_dataset(valueList.type) # not implemented for python
          #if type_info:
          #  print type_info
          try:
            # prepend value type
            field_name = type_info[midx][0] + "_" + metricName
          except:
            field_name = metricName + str(midx)

        # for measurements with values per CPU
        # endswith('cpu') covers CPU usage collector and likwid per cpu metrics
        cpu = ""
        if measurement.endswith('cpu') or measurement.endswith('_socket'): 
          # generate cpu tag from last value (compare last timestamp)
          tags["cpu"] = last_cpu # add CPU number as tag
          # add CPU number as tag 
          if valueList.plugin_instance:
            cpu = _getInteger(valueList.plugin_instance)

        # for measurements with values per GPU
        gpu = ""
        if field_name.startswith('gpu#'):
          # split metric name
          mname_list = field_name.split('.')
          
          # remove gpu#* prefix from metric name
          gpu = mname_list.pop(0)
          
          # generate GPU ID tag
          if last_gpu != "":
              tags["gpu"] = last_gpu.replace("gpu#","")
          else:
              tags["gpu"] = gpu.replace("gpu#","")
          
          if mname_list > 1:
              field_name = ".".join(mname_list)
          else:
              field_name = mname_list[0]

        # if possible, write all fields in a single line
        # if next value has the same timestamp and GPU, add it as another field
        if last_valueList and time == last_valueList.time and cpu == last_cpu and gpu == last_gpu:
          # add field values to measurement point
          fields[field_name] = value
        else:
          if fields: # fields are available, but time changed
            # write last fields with last timestamp
            last_tags = {"hostname": last_valueList.host}
            if last_gpu != "":
              last_tags["gpu"] = last_gpu.replace("gpu#","")
            if last_cpu != "":
              last_tags["cpu"] = last_cpu
              
            #collectd.info("Data point: %s:%d, tags: %s, fields: %s" % (measurement, int(last_valueList.time), str(last_tags), str(fields)))
            
            metrics.append({
                "measurement": measurement,
                "time": int(last_valueList.time),
                "tags": last_tags,
                "fields": fields}
                )

          # write first field value for next measurement point
          fields = {field_name: value}

        # remember last metric values
        last_cpu = cpu
        last_gpu = gpu
        last_valueList = valueList

    # write remaining fields
    if fields:
      if "gpu" in tags:
        tags["gpu"] = last_gpu.replace("gpu#","")
      #elif 'cpu' in tags:
      #  tags["cpu"] = last_cpu.replace("cpu","")
        
      #collectd.info("Remaining data point: %s:%d, tags: %s, fields: %s" % (measurement, int(last_valueList.time), str(tags), str(fields)))
      metrics.append({
          "measurement": measurement,
          "time": int(last_valueList.time),
          "tags": tags,
          "fields": fields}
          )

  return metrics
  
def flush(timeout, identifier):
  global batch_count
  collectd.info("[InfluxDB Writer] Flush metrics in batch with size %d." % (batch_count))

  # Send pickled batch
  _send()
    
collectd.register_config(set_config)
collectd.register_write(write)
collectd.register_init(init_callback)
collectd.register_flush(flush)
