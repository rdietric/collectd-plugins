# coding=utf-8

"""
Collect data from InfiniBand devices. 
!!! This plugin resets the InfiniBand counters, when initialized!!!

by Robert Dietrich (robert.dietrich@tu-dresden.de) for the ProPE project

#### Dependencies

 * [subprocess](http://docs.python.org/library/subprocess.html)
"""

import time
import os
import sys
import subprocess
import re

try:
  import collectd
except ImportError:
  import dummy_collectd as collectd
  collectd.info("Using dummy collectd for testing")

# get available file systems
from subprocess import Popen, PIPE, STDOUT

### global variables ###
directory = "/sys/class/infiniband"
devices = None

enabled = False
num_reads = 0
recheck_limit = 0 # number of intervals/collects after re-checking the available IB devices (default is off: 0)

ibPortList = []

# values of previous counter read
recv_prev = sys.maxsize
send_prev = sys.maxsize
time_prev = 0

perfquery_filepath = "/usr/sbin/perfquery"
### END: global variables ###

### utility functions
def is_exe(fpath):
  return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

def which(program):
  fpath, fname = os.path.split(program)
  if fpath:
    if is_exe(program):
      return program
  else:
    for path in os.environ["PATH"].split(os.pathsep):
      exe_file = os.path.join(path, program)
      if is_exe(exe_file):
        return exe_file

  return None
##################### 

# reset counters, if perfquery is available
def _reset_counters():
  if perfquery_filepath:
    collectd.debug("[Infiniband Plugin] Reset IB counters!")
    try:
      proc = subprocess.Popen([perfquery_filepath], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
      (out, err) = proc.communicate()
    except subprocess.CalledProcessError as e:
      collectd.info("[Infiniband Plugin] %s error launching: %s; skipping" % (perfquery_filepath, e))
      return -1
    else:
      if proc.returncode:
        collectd.error("[Infiniband Plugin] %s return exit value %s; skipping" % (perfquery_filepath, proc.returncode))
        return -1
      if err:
        collectd.error("[Infiniband Plugin] %s return error output: %s" % (perfquery_filepath, err))
        return -1
  else:
    collectd.info("[Infiniband Plugin] Cannot reset counters!" )
    
"""
brief Determine the files and paths where the IB counters are read from. 
Find infiniband devices, if they have not been specified in the collectd.conf. 

Directory where infiniband devices are located, default: /sys/class/infiniband
"""
def _setupIBfiles():
  # if no devices are explicitly specified, detect them
  if devices == None:
    if not os.path.isdir( directory ):
      collectd.error("[InfiniBand Plugin] Infiniband directory %s does not exist!" % (directory,))
      return -1

    # find all infiniband devices
    cmd = "find " + directory + "/* -maxdepth 0"
    try:
      p = Popen( cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE )
      detectedDevices, stderr = p.communicate()
    except subprocess.CalledProcessError as e:
      collectd.info("[Infiniband Plugin] %s error launching: %s; skipping" % (repr(e), cmd))
      return 0
    else:
      detectedDevices = detectedDevices.decode('utf-8')
      ibDevices = filter( None, detectedDevices.split('\n') )
  else:
    # devices from collectd.conf are a comma-separated list
    ibDevices = devices.split(',')

  # find ports for all devices and add them to the list
  global ibPortList
  for ibDevice in ibDevices:
    if not os.path.isdir( ibDevice + "/ports" ):
      collectd.info("[InfiniBand Plugin] No ports for IB device %s found" % (ibDevice,))
      continue

    collectd.debug("[InfiniBand Plugin] Found IB device with ports: " + ibDevice)

    cmd = "find " + ibDevice + "/ports/* -maxdepth 0 -type d 2>/dev/null"
    try:
      p = Popen( cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE )
      ibDevicePorts, stderr = p.communicate()
    except subprocess.CalledProcessError as e:
      collectd.info("[Infiniband Plugin] %s error launching: %s; skipping" % (repr(e), cmd))
      return 0
    else:
      ibDevicePorts = ibDevicePorts.decode('utf-8')
      
    for ibDevicePort in filter( None, ibDevicePorts.split('\n') ):
      if not os.path.isdir( ibDevicePort + "/counters" ):
        collectd.info("[Infiniband Plugin] No counters for IB device port %s found." % (ibDevicePort,))
        continue

      ibPortList.append( ibDevicePort )
      collectd.debug("[InfiniBand Plugin] Found IB port with counters: " + ibDevicePort)

  if len(ibPortList) == 0:
    collectd.info("[Infiniband Plugin] No IB devices/ports found!" )

  return 0

def _read_counter(file):
  try:
    # Total number of data octets, divided by 4 (lanes), received on all VLs. This seems to be a32 bit unsigned counter.
    f = open( file, "r" )
    finput = f.read()
    f.close()
  except IOError as ioe:
    collectd.error("[InfiniBand Plugin] Cannot read IB %s counter: %s" % (file, repr(ioe)) )
  else:
    return float(finput)

  return float(-1)

def ib_plugin_config(config):
  if config.values[0] == 'ib_bw':
    collectd.info("[InfiniBand Plugin] Get configuration")
    for value in config.children:
      if value.key == 'directory':
        global directory
        directory = value.values[0]
        collectd.info("[InfiniBand Plugin] Use directory %s from config file" % (directory,))
      elif value.key == 'devices':  # InfiniBand devices (comma separated)
        global devices
        devices = value.values[0]
        collectd.info("[InfiniBand Plugin] Use ib_devices %s from config file" % (devices,))
      elif value.key == 'recheck_limit':
        global recheck_limit
        recheck_limit = int(value.values[0])
        if recheck_limit > 0:
          collectd.info("[InfiniBand Plugin] Check for available IB devices every %d collects" % (recheck_limit,))
      

def ib_plugin_initialize():
  collectd.debug("[InfiniBand Plugin] Initialize ...")

  # check for perfquery
  global perfquery_filepath
  if not perfquery_filepath or not is_exe(perfquery_filepath):
    perfquery_filepath = which("perfquery")

  if perfquery_filepath:
    collectd.debug("[InfiniBand Plugin] %s is available to reset counters" % (perfquery_filepath,))

    # add -R option to reset counters
    perfquery_filepath += " -R"

  # initial reset of IB counters
  _reset_counters()
  
  # determine the paths to the IB counter files
  global enabled
  if _setupIBfiles() >= 0:
    enabled = True
  else:
    enabled = False
    #raise collectd.CollectdError

"""
brief Read send and receive counters from Infiniband devices
"""
def ib_plugin_read(data=None):
  # check for available IB files every #recheck_limit reads
  global num_reads
  num_reads += 1
  
  if num_reads == recheck_limit: 
    _setupIBfiles()
    num_reads = 0

  if not enabled:
    return
  
  # set receive and send values to zero
  recv = 0
  send = 0

  overflow = False

  # one time stamp for all IB metrics
  timestamp = time.time()

  # iterate over all ports (of all devices)
  for ibPort in ibPortList:
    # get port receive data
    counter_value = _read_counter(ibPort + "/counters/port_rcv_data")
    if counter_value == -1:
      continue

    # check if counter value stops at 32 bit and reset it
    if counter_value == 4294967295:
      overflow = True
    else:
      recv += counter_value * 4

    # get port send data
    try:
      # Total number of data octets, divided by 4 (lanes), transmitted on all VLs. This seems to be a32 bit unsinged counter.
      f = open( ibPort + "/counters/port_xmit_data", "r" )
      finput = f.read()
      f.close()
    except IOError as ioe:
      collectd.error("[Infiniband Plugin] Cannot read IB xmit counter: %s" % (repr(ioe),))
    else:
      counter_value = float(finput)

      # check if counter value stops at 32 bit and reset it
      if counter_value == 4294967295:
        overflow = True

      send += counter_value * 4

  global send_prev, recv_prev, time_prev
  if overflow:
    _reset_counters()

    # set new previous values
    recv_prev = 0
    send_prev = 0
  else:
    if recv >= recv_prev and send >= send_prev:
      ib_bw = ( recv - recv_prev + send - send_prev ) / ( timestamp - time_prev )

      # TODO: change to derive type, no need to store prev values
      vl = collectd.Values(type='gauge')
      vl.plugin='infiniband'
      vl.values = [ib_bw]
      vl.time = timestamp
      vl.type_instance = 'bw'
      vl.dispatch()

    # set new previous values
    recv_prev = recv
    send_prev = send

  time_prev = timestamp

# paste on command line
#echo "PUTNOTIF severity=okay time=$(date +%s) message=hello" | socat - UNIX-CLIENT:/home/rdietric/sw/collectd/5.8.0/var/run/collectd-unixsock
def ib_plugin_notify(notification, data=None):
  #collectd.info("[Infiniband Plugin] Notification: %s" % (str(notification),))

  # try:
  #   if notification.plugin == 'ib_bw':
  #     collectd.info("[Infiniband Plugin] target plugin ...")
  #   else:
  #     collectd.info("[Infiniband Plugin] other plugin ..." % (str(notification.plugin),))
  # except:
  #   collectd.error("[Infiniband Plugin] no target plugin specified")

  # for severity failure (1), try to re-start plugin
  if notification.severity == 1 and notification.message == "start":
    collectd.info("[Infiniband Plugin] Restart ...")
    try:
      collectd.unregister_read(ib_plugin_read)
    except:
      collectd.error("[Infiniband Plugin] Could not unregister read callback!")
    ib_plugin_initialize()

  # for severity okay (4)
  if notification.severity == 4 and notification.message == "check":
    collectd.info("[Infiniband Plugin] Check IB files ...")
    _setupIBfiles()
    global num_reads
    num_reads = 0

if __name__ != "__main__":
  # when running inside plugin register each callback
  collectd.register_config(ib_plugin_config)
  collectd.register_init(ib_plugin_initialize)
  collectd.register_notification(ib_plugin_notify)

  # always register read plugin, which triggers the file checks once a while
  collectd.register_read(ib_plugin_read)
else:
  # outside plugin just collect the info
  ib_plugin_initialize()
  ib_plugin_read()
  if len(sys.argv) < 2:
      while True:
          time.sleep(10)
          ib_plugin_read()
          
