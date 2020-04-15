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
import subprocess
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
batch_derive = {} # storage for previous value lists of derived/counter types

#### Mapping of HW threads to cores ####
per_core_plugins = None
per_core_avg_plugins = None

# default: SMT is disabled: no. of HW threads == no. of physical cores 
threads_per_core = 1

# hardware thread ID is provided by the OS contiguous, starting from zero
coreMapping = None

# timestamp of the current group of values (see write() and _collect()) with seconds precision
currentTimestamp = 0

#num_aggregated = 0
########################################

time_precision = 's'


"""
Connect to the InfluxDB server
"""
def _connect():
  try:
      # Open Connection
      global influx
      influx = InfluxDBClient(host=hostname, port=port, username=username, 
                              password=password, database=database, ssl=ssl)
      
      collectd.info("InfluxDB write: established connection to %s:%d/%s." % (hostname, port, database) )
  except Exception as ex:
      # Log Error
      collectd.info("InfluxDB write: failed to connect to %s:%s/%s. (%s:%s) - %s" % (hostname, port, database, username, password, ex) )
      _close()

"""
Close the socket = do nothing for influx which is http stateless
"""
def _close():
    global influx
    influx = None

"""
Mapping of HW threads to CPU cores (via parsing the output of likwid-topology)
"""
def _setHWThreadMapping():
  cmd = 'likwid-topology -O' # comma separated topology output

  try:
    status, result = subprocess.getstatusoutput(cmd)
  except Exception as ex:
    collectd.info("InfluxDB write: error launching '%s': %s" % (cmd, repr(ex)))
    return False

  # a zero status means without errors, 13 means permission denied (maybe only for some mounts)
  if status != 0 and status != 13:
    collectd.info("InfluxDB write: get HW thread mapping failed (status: %s): %s" % (status, result))
    return False

  startIdx = 0
  num_threads = 0
  lines = result.split('\n')

  # determine start and end of thread mapping lines
  for line in lines:
    if line.startswith("Threads per core:"):
      global threads_per_core
      threads_per_core = int(re.search(r'\d+', line).group())
      if threads_per_core == 1:
        return False

    startIdx += 1
    if line.startswith('TABLE,Topology,'):
      num_threads = int(re.search(r'\d+', line).group())
      startIdx += 1 # skip table header
      break

  # initialize and fill mapping array
  global coreMapping
  coreMapping = [None]*num_threads
  for line in lines[startIdx:startIdx+num_threads]:
    v = line.split(',')
    try:
      coreMapping[int(v[0])] = v[2]
      #coreMapping[v[0]] = v[2]
      collectd.info("InfluxDB write: HW thread {:3d} -> Core {:3d}".format(int(v[0]), int(v[2])))
    except:
      collectd.info("InfluxDB write: HWThread-to-core mapping out of bound error")
      return False

  return True


"""
brief: Store values per plugin instance. 

Value lists are stored per plugin and plugin instance. The plugin instance is 
used as a tag. For per-core plugins (see configuration), the plugin instance is 
assumed to be the processor ID (given by the OS). 

Return True, if a value has been added to the batch, otherwise False.
"""
def _collect(valueList):
  global batch

  if valueList.plugin: 
    plugin_name = valueList.plugin
  else:
    collectd.error('InfluxDB writer: plugin member is required!')
    return False

  tag = valueList.plugin_instance

  # first check for the tag, which is None for many plugins
  is_per_core = tag and per_core_plugins and valueList.plugin in per_core_plugins

  # map to core
  if is_per_core:
    #collectd.info("value: " + str(valueList))
    tag = coreMapping[int(tag)]
    valueList.plugin_instance = tag

  # create array for plugin and tag, if it is not available yet
  if plugin_name in batch:
    if tag in batch[plugin_name]:
      # aggregate (sum up) per core, if configured
      if is_per_core:
        # iterate reversed as matches are most probable at the end of the list
        # lists should be very short 
        valueListTimeInt = int(valueList.time)
        for vlStored in reversed(batch[plugin_name][tag]):
          if vlStored.type == valueList.type and vlStored.type_instance == valueList.type_instance and int(vlStored.time) == valueListTimeInt:
            for idx in range(len(vlStored.values)):
              vlStored.values[idx] += valueList.values[idx]
              #global num_aggregated
              #num_aggregated += 1
            return False

      # append value
      batch[plugin_name][tag].append(valueList)
    else:
      # create array of values for new tag
      batch[plugin_name][tag] = [valueList]
  else:
    # add the plugin and the tag with a new value
    batch[plugin_name] = {tag:[valueList]}

  return True


"""
Send data to InfluxDB. Data that cannot be sent will be kept in cache.
"""
def _send():
  global batch
  global batch_count
  #global num_aggregated

  if not influx:
    collectd.info('InfluxDB write: connection not available. Try reconnect ...')
    _connect()

  metrics = _prepare_metrics()

  # reset batch which only contains initial values of derived metrics
  if len(metrics) == 0:
    batch = {}
    batch_count = 0
    if len(batch_derive) == 0:
      collectd.info('InfluxDB write: no metrics to send. '
        'No previous values are stored. Should not happen!')
    return

  # Send data to InfluxDB (len(metrics) <= batch_count as NaN and inf are not moved from batch to metrics)
  collectd.info('InfluxDB write: %d lines (%d series)' % (len(metrics), batch_count))
  #collectd.info('InfluxDB write: %d lines (%d series incl. %d rates), %d aggregated' % (len(metrics), batch_count, len(batch_derive), num_aggregated) )
  #collectd.info(str(metrics))

  ret = False

  if influx:
    try:
      ret = influx.write_points(metrics, time_precision=time_precision)
    except Exception as ex:
      collectd.error("InfluxDB write: error sending metrics(%s)" % (ex,))
      #raise

  # empty batch buffer for successful writes
  if ret:
    #collectd.info("reset batch")
    batch = {}
    batch_count = 0
    #num_aggregated = 0

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
            # plugin instance is processor ID (given by OS)
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
          collectd.info("InfluxDB write: no values available for %s:%s!" % (measurement,metricName))
          continue

        for midx, value in enumerate(valueList.values):
          # ignore invalid values
          if str(value) == "nan" or math.isnan(float(value)) or str(value) == "inf":
            #collectd.info("Found invalid value!")
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

          # build average per core for respectively configured metrics
          # (assumes that no HW thread value got lost within a time group)
          if per_core_avg_plugins and measurement in per_core_avg_plugins:
            #collectd.info("divide by thread/core: %s:%s = %f/%d=%f!" % (measurement,metricName,value, threads_per_core, value/threads_per_core) )
            value /= threads_per_core

          #### for derived counters ####
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
                  collectd.warning("InfluxDB write: found a previous value "
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

"""
Extract integer value from string
"""
intPattern = re.compile(r'\d')
def _getInteger(stringInt):
  ret = None
  try:
    ret = int(stringInt)
  except: # ValueError as e:
    ret = intPattern.match(stringInt)
    if ret:
      return int(ret.group())
    
  return ret

############################################
##### Start Collectd callback routines #####
"""
Set plugin configuration (from collectd config file).
"""
def set_config(config):
  if config.values[0] == 'influx_write':
    collectd.info("InfluxDB write: get configuration")
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
          collectd.info("InfluxDB write: store rates for derived and counter types")
      elif value.key == 'PerCore':
        if _setHWThreadMapping():
          global per_core_plugins
          global per_core_avg_plugins
          per_core_avg_plugins = []
          for value in value.values:
            #collectd.info(value)
            v = value.split(':')
            if per_core_plugins is None:
              per_core_plugins = []
            per_core_plugins.append(v[0])

            if v[1] == 'avg':
              if per_core_avg_plugins is None:
                per_core_avg_plugins = []
              per_core_avg_plugins.append(v[0])
      else:
        collectd.info("InfluxDB write: ignore unknown option %s" % (value.key,))

"""
Collectd initialization callback.
Responsible for starting the sending thread
"""
def init_callback():
  global InfluxDBClient
  if not InfluxDBClient:
    collectd.info('InfluxDB write: influxdb.client.InfluxDBClient import failed.')
  else:
    #collectd.info('[InfluxDB Writer] Initialize.')
    _connect()


"""
Collectd write callback.
Retrieves values from read plugins.
"""
def write(valueList, data=None):
  if not InfluxDBClient:
    return

  #collectd.info('InfluxDB write: %s' % (str(valueList),))
  #if data:
  #  collectd.info('[InfluxDB Writer] Data: %s' % (str(data),))

  # cut fraction of seconds (required to group values from e.g. cpu plugin, 
  # where values from different HW threads have differ in the fractional part
  # of the timestamp)
  vlTime = int(valueList.time)
  global currentTimestamp
  if currentTimestamp == 0:
    currentTimestamp = vlTime
    #collectd.info("InfluxDB write: group time {:d}".format(currentTimestamp))

  # check for changed timestamp before sending to make sure that all values in
  # current time period (second) are aggregated
  global batch_count
  if currentTimestamp != vlTime:
    currentTimestamp = vlTime
    #collectd.info("InfluxDB write: group time {:d}".format(currentTimestamp))
    if batch_count >= batch_size: 
      #collectd.info("InfluxDB write: sending batch of {:d}".format(batch_count))
      _send()

  # Add data to global batch
  if batch_count <= cache_size:
    if _collect(valueList):
      batch_count += 1
      #collectd.info("batch count: " + str(batch_count))
  
def flush(timeout, identifier):
  global batch_count
  collectd.info("InfluxDB write: flush {:d} values".format(batch_count))

  # Send pickled batch
  _send()
    
# register Collectd callbacks
collectd.register_config(set_config)
collectd.register_write(write)
collectd.register_init(init_callback)
collectd.register_flush(flush)
