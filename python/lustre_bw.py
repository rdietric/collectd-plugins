# coding=utf-8

"""
Collect data from Lustre file system

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

### constants ###
# number of array entries per file system and positions
FS_ENTRIES = 4 # tuple of three
POS_FSNAME = 1
POS_ENABLED = 2
POS_PREV_DATA = 3

_KEY_MAPPING = [
  'open',
  'close',
  'fsync',
  'create',
  'seek'
]
### END: constants ###

### global variables ###
enabled = False

lustre_paths = None

extents_stats = False

time_prev = 0

# file systems info array: 
# 3 entries per file system (full file system path, file system name, dict of last metrics values)
fsInfo = []
        
num_reads = 0
recheck_limit = 0 # number of intervals/collects after re-checking the available file systems (default is off: 0)
### END: global variables ###

# Return an array of lustre file system paths (either from config file or by searching in /proc/fs/lustre/llite/)
def _getLustreFileSystemPaths():
  if lustre_paths != None:
    collectd.debug( "[Lustre Plugin] Use lustre paths %s from config file" % (lustre_paths,))
    return lustre_paths.split(',')
  else:
    # find file systems
    #cmd = 'find /proc/fs/lustre/llite/* -maxdepth 0 -type d 2>/dev/null'
    cmd = 'ls /proc/fs/lustre/llite/'
    try:
      p = subprocess.Popen( cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE )
      stdout, stderr = p.communicate()
    except subprocess.CalledProcessError as e:
      collectd.info("[Lustre Plugin] %s error launching: %s; skipping" % (repr(e), cmd))
      return []
    else:
      stdout= stdout.decode('utf-8')

    if stdout == '':
      return []

    collectd.debug("[Lustre Plugin] Found lustre file system paths: %s" % (stdout,))
    
    fsArray = stdout.split('\n')
    fsArray.remove("") # remove empty string

    return fsArray

def _setupLustreFiles():
  global fsInfo
  fsInfo = []
        
  for fsPath in _getLustreFileSystemPaths():
    if not fsPath:
      continue

    p_start = fsPath.rfind('/')
    p_end   = fsPath.rfind('-')

    # no '/' found
    if p_start == -1:
      continue

    # mn '-' found
    if p_end == -1:
      p_end = fsPath.len()

    collectd.debug("[Lustre Plugin] Collect data for file system: %s" % (fsPath[p_start+1:p_end],))

    fsInfo.append( fsPath ) # full path to the file system /proc information files
    fsInfo.append( fsPath[ p_start + 1 : p_end ] ) # name of file system, e.g. scratch
    fsInfo.append( False ) # first, disable the file system

    # append array entry for lustre offset dictionary
    fsInfo.append( {} )

  collectd.debug("[Lustre Plugin] Found %d file systems" % (len(fsInfo) / FS_ENTRIES,))

  # gather first/prev values
  if len(fsInfo) > 0:
    _setPrevValues()
  else:
    global enabled
    enabled = False

  return len(fsInfo)

        
# set initial values for each file system
def _setPrevValues():
  global enabled
  for idx in xrange( 0, len(fsInfo)-1, FS_ENTRIES):
    fs = fsInfo[ idx ]
    
    if not fs:
      continue
      
    # add lustre stats offsets
    statFile = fs + "/stats"
    try:
      f = open( statFile, "r" )
      finput = f.read()
      f.close()
    except IOError as ioe:
      collectd.debug( "[Lustre Plugin] Cannot read from stats file: %s" % (repr(ioe),))
      fsInfo[ idx + POS_ENABLED ] = False
      continue
    else:
      enabled = True
      fsInfo[ idx + POS_ENABLED ] = True
      stats_offsets = _parseLustreStats( finput )
      fsInfo[ idx + POS_PREV_DATA ].update( stats_offsets )

    # add lustre extents_stats offsets
    if extents_stats:
      statFile = fs + "/extents_stats"
      try:
        f = open( statFile, "w+" )
        finput = f.readline()
        if finput.startswith("disabled"):
          f.write("1")
          collectd.debug("[Lustre Plugin] Enabled extents_stats for %s" % (fs,))
        f.close()
      except IOError as ioe:
        collectd.debug("[Lustre Plugin] Cannot read/enable extents_stats: %s" % (repr(ioe),))
      else:
        fsInfo[ idx + POS_PREV_DATA ].update(_parseLustreExtendsStats(finput))

    # store timestamp of previous data
    global time_prev
    time_prev = time.time()
        
# check if there are file systems available, which are not monitored yet
def _haveNewFS():        
  #self.log.debug( "New lustre FS? %s", self._getLustreFileSystemPaths() )
  for fsPathNew in _getLustreFileSystemPaths():        
    newFS=True
    # mark the FS as not new, if it is the current list
    for idx in xrange( 0, len(fsInfo)-1, FS_ENTRIES):
      fsPathCurr = fsInfo[ idx ]
      
      if fsPathCurr == fsPathNew:
        newFS = False
        break
    
    if newFS:
      collectd.debug("[Lustre Plugin] Found new file system %s!" % (fsPathNew,))
      return True
    
  return False

# Parse the lustre stats file
# return dictionary with metric names (key) and value (value)
# TODO: catch index out of bound exceptions if stats file format changes
def _parseLustreStats(finput):
  lustrestat = {}
  for line in filter( None, finput.split('\n') ):
    linelist = line.split() #re.split( "\s+", line ) #split is faster than re.split
    if linelist[0] == "read_bytes":
      lustrestat["read_requests"] = float(linelist[1]) #do not record, can be generated from extended stats
      lustrestat["read_bw"] = float(linelist[6])
    elif linelist[0] == "write_bytes":
      lustrestat["write_requests"] = float(linelist[1]) #do not record, can be generated from extended stats
      lustrestat["write_bw"] = float(linelist[6])
    elif linelist[0] in _KEY_MAPPING:
      lustrestat[linelist[0]] = float(linelist[1])

  return lustrestat

# parse the input from lustre extents_stats file
# return dictionary with metric names (key) and value (value)
def _parseLustreExtendsStats(finput):
  lustrestat = {}
  for line in filter(None, finput.split('\n')):
    #self.log.debug(line)
    # split (by whitespace) into array
    values = line.split() #split is faster than re.split

    #ignore non-values lines (value lines have 11 values)
    #if pattern_value.match(line): #savely identify values lines
    if len(values) != 11:  #fast access values lines
      continue

    #self.log.debug(values)
    try:
      # reads
      value = float(values[4])
      if value > 0:
          lustrestat[ "read_"+values[0]+"-"+values[2] ] = value

      # writes
      value = float(values[8])
      if value > 0:
          lustrestat[ "write_"+values[0]+"-"+values[2] ] = value
    except ValueError as ve:
      collectd.error("[Lustre Plugin] Could not convert to float (%s)" % (repr(ve),))

  return lustrestat

def _publishLustreMetrics(fsIdx, lustreMetrics, timestamp): 
    fsname   = fsInfo[ fsIdx + POS_FSNAME ]
    previous = fsInfo[ fsIdx + POS_PREV_DATA ]

    # append file system name to metric name (to further specify measurements)
    fsname = '<' + fsname + '>'

    interval = timestamp - time_prev

    # for all lustre metrics
    for metric in lustreMetrics.keys():
      #self.log.debug( "lustre_" + fsname + "_" + metric )
      
      # determine bandwidth
      if previous.has_key( metric ):
          currValue = lustreMetrics[ metric ] - previous[ metric ]
          #self.log.debug( "Current value: %d (%d - %d)", currValue, lustreMetrics[ metric ], previous[ metric ] )
      else:
          currValue = lustreMetrics[ metric ]
          #self.log.debug( "Current value (no offset): %d", currValue )

      # set previous value
      previous[ metric ] = lustreMetrics[ metric ]
      
      if currValue >= 0:
        # TODO: change to derive type, no need to store prev values
        vl = collectd.Values(type='gauge')
        vl.plugin='lustre_' + fsname
        vl.type='lustre'
        vl.values = [float(currValue) / float(interval)]
        vl.time = timestamp
        vl.type_instance = metric
        vl.dispatch()
      else:
        collectd.debug("[Lustre Plugin] %d: derivative < 0 (current: %f, previous available? %s" % (timestamp, lustreMetrics[ metric ], previous.has_key( metric )))

def lustre_plugin_config(config):
  if config.values[0] == 'lustre_bw':
    collectd.info("[Lustre Plugin] Get configuration")
    for value in config.children:
      if value.key == 'path':
        global lustre_paths
        lustre_paths = value.values[0]
        collectd.info("[Lustre Plugin] Paths to lustre file systems: %s" % (lustre_path,))
      elif value.key == 'extents_stats':
        global extents_stats
        extents_stats = value.values[0]
        collectd.info("[Lustre Plugin] extents_stats: %s" % (extents_stats,))
      elif value.key == 'recheck_limit':
        global recheck_limit
        recheck_limit = int(value.values[0])
        if recheck_limit > 0:
          collectd.info("[Lustre Plugin] Check for available lustre file systems every %d collects" % (recheck_limit,))
      

def lustre_plugin_initialize():
  collectd.debug("[Lustre Plugin] Initialize ...")

  collectd.info("Python version: %d.%d.%d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))

  # setup lustre file paths and initialize previous values
  _setupLustreFiles()


"""
brief Read send and receive counters from Infiniband devices
"""
def lustre_plugin_read(data=None):
  #self.log.debug( "Collect %d ? %d", num_reads, recheck_limit)

  # check for available file systems every #recheck_limit reads
  global num_reads
  num_reads += 1
  
  # check for available file systems
  if num_reads == recheck_limit: 
    if _haveNewFS():
      _setupLustreFiles()
      return
        
    # reset check counter
    num_reads = 0

  if not enabled:
    return

  collectd.debug("[Lustre Plugin] Collect for %d file systems" % (len(fsInfo) / FS_ENTRIES),)

  # get time stamp for all lustre metric values that we read
  timestamp = time.time()

  # iterate over file system info list in steps of FS_ENTRIES (as we have FS_ENTRIES entries per file system)
  #self.log.debug("[LustreCollector] %d, %d", len(self.fsInfo)-1, FS_ENTRIES)
  for idx in xrange( 0, len(fsInfo)-1, FS_ENTRIES):
    # skip disabled file systems
    if fsInfo[ idx + POS_ENABLED ] == False:
      continue

    fs = fsInfo[ idx ]
    if not fs:
      continue

    #self.log.debug("[LustreCollector] Collect from lustre %s (idx: %d), %d metrics", fs, idx, len(self.fsInfo[idx+2]))
    statFile = fs + "/stats"
    try:
      f = open( statFile, "r" )
      finput = f.read()
      f.close()
    except IOError as ioe:
      collectd.debug("[Lustre Plugin] Cannot read from stats file: %s" % (repr(ioe),))
    else:
      # parse the data into dictionary (key is metric name, value is metric value)
      lustrestat = _parseLustreStats( finput )

      # publish the metrics
      _publishLustreMetrics( idx, lustrestat, timestamp )

    if extents_stats:
      statFile = fs + "/extents_stats"
      try:
          f = open( statFile, 'r' )
          finput = f.read()
          f.close()
      except IOError as ioe:
        collectd.debug( "[Lustre Plugin] Cannot read from extents_stats file: %s" % (repr(ioe),))
      else:
        # parse the data into dictionary (key is metric name, value is metric value)
        lustrestat = _parseLustreExtendsStats( finput )

        _publishLustreMetrics( idx, lustrestat, timestamp )

  global time_prev
  time_prev = timestamp

# paste on command line
#echo "PUTNOTIF severity=okay time=$(date +%s) message=hello" | socat - UNIX-CLIENT:/home/rdietric/sw/collectd/5.8.0/var/run/collectd-unixsock
def lustre_plugin_notify(notification, data=None):
  #collectd.info("[Lustre Plugin] Notification: %s" % (str(notification),))
  
  # for severity okay (4)
  if notification.severity == 4 and notification.message == "check":
    collectd.info("[Lustre Plugin] Check Lustre files ...")
    if _haveNewFS():
      _setupLustreFiles()
        
    # reset check counter
    global num_reads
    num_reads = 0


if __name__ != "__main__":
  # when running inside plugin register each callback
  collectd.register_config(lustre_plugin_config)
  collectd.register_init(lustre_plugin_initialize)
  collectd.register_read(lustre_plugin_read)
  collectd.register_notification(lustre_plugin_notify)
else:
  # outside plugin just collect the info
  lustre_plugin_initialize()
  lustre_plugin_read()
  if len(sys.argv) < 2:
      while True:
          time.sleep(10)
          lustre_plugin_read()
          
