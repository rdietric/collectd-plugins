#!/usr/bin/python3
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

DEFAULT_LUSTRE_SEARCH_PATHS=['/sys/kernel/debug/lustre/llite/','/proc/fs/lustre/llite/']
### END: constants ###

### global variables ###
enabled = False

lustre_paths = None

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
    #cmd = 'find ' + DEFAULT_LUSTRE_SEARCH_PATH + '* -maxdepth 0 -type d 2>/dev/null'
    # list the full paths
    for searchPath in DEFAULT_LUSTRE_SEARCH_PATHS:
      if not os.path.exists(searchPath):
        continue
      cmd = 'ls -d ' + searchPath + '*'
      try:
        p = subprocess.Popen( cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE )
        stdout, stderr = p.communicate()
      except subprocess.CalledProcessError as e:
        collectd.info("[Lustre Plugin] %s error launching: %s; skipping" % (repr(e), cmd))
        return []
      else:
        stdout= stdout.decode('utf-8')

      if stdout == '':
        collectd.info("[Lustre Plugin] No file systems found: %s" % (cmd,))
        return []

      collectd.info("[Lustre Plugin] Found lustre file system paths: %s" % (stdout,))
      
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
      collectd.info("[Lustre Plugin] no start slash")
      continue

    # mn '-' found
    if p_end == -1:
      p_end = fsPath.len()

    collectd.info("[Lustre Plugin] Collect data for file system: %s" % (fsPath[p_start+1:p_end],))

    fsInfo.append( fsPath + '/stats' ) # full path to the Lustre stats file
    fsInfo.append( fsPath[ p_start + 1 : p_end ] ) # name of file system, e.g. scratch
    fsInfo.append( False ) # first, disable the file system

    # append array entry for lustre offset dictionary
    fsInfo.append( {} )

  #collectd.info("[Lustre Plugin] Found %d file systems" % (len(fsInfo) / FS_ENTRIES,))

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
  for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
    statsFile = fsInfo[ idx ]
    
    if not statsFile:
      continue
      
    # add lustre stats offsets
    try:
      f = open( statsFile, "r" )
      finput = f.read()
      f.close()
    except IOError as ioe:
      collectd.info( "[Lustre Plugin] Cannot read from stats file: %s (%s)" % (statsFile, repr(ioe),))
      fsInfo[ idx + POS_ENABLED ] = False
      continue
    else:
      enabled = True
      fsInfo[ idx + POS_ENABLED ] = True
      stats_offsets = _parseLustreStats( finput )
      fsInfo[ idx + POS_PREV_DATA ].update( stats_offsets )

    # store timestamp of previous data
    global time_prev
    time_prev = time.time()
        
# check if there are file systems available, which are not monitored yet
def _haveNewFS():        
  #collectd.debug( "New lustre FS? %s", _getLustreFileSystemPaths() )
  for fsPathNew in _getLustreFileSystemPaths():
    newFS=True
    # mark the FS as not new, if it is in the current list
    for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
      fsPathCurr = fsInfo[ idx ]
      
      if fsPathCurr == fsPathNew:
        newFS = False
        break
    
    if newFS:
      collectd.info("[Lustre Plugin] Found new file system %s!" % (fsPathNew,))
      return True
    
  return False

# Check for the existence of the stats files
def _checkLustreStatsFiles():
  # iterate over file system info list in steps of FS_ENTRIES
  for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
    # disable file system, if stats file does not exist
    if not os.path.isfile(fsInfo[idx]):
      fsInfo[idx + POS_ENABLED] = False
      collectd.warning("[Lustre Plugin] %s does not exist. Disable file system monitoring." % (fsInfo[idx],))
    elif fsInfo[idx + POS_ENABLED] == False:
      fsInfo[idx + POS_ENABLED] = True
      collectd.info("[Lustre Plugin] %s re-enable." % (fsInfo[idx],))

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

def _publishLustreMetrics(fsIdx, lustreMetrics, timestamp): 
    fsname   = fsInfo[ fsIdx + POS_FSNAME ]
    previous = fsInfo[ fsIdx + POS_PREV_DATA ]

    interval = timestamp - time_prev

    # for all lustre metrics (iterate over keys)
    for metric in lustreMetrics:      
      ### determine bandwidth manually ###
      # check for a previous value
      if metric in previous:
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
        vl.values = [float(currValue) / float(interval)]
        vl.time = timestamp
        vl.type_instance = metric
        vl.dispatch()
      else:
        collectd.debug("[Lustre Plugin] %d: derivative < 0 (current: %f, previous available? %s" % (timestamp, lustreMetrics[ metric ], metric in previous))

def lustre_plugin_config(config):
  if config.values[0] == 'lustre_bw':
    collectd.info("[Lustre Plugin] Get configuration")
    for value in config.children:
      if value.key == 'path':
        global lustre_paths
        lustre_paths = value.values[0]
        collectd.info("[Lustre Plugin] Paths to lustre file systems: %s" % (lustre_path,))
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

  _checkLustreStatsFiles()


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
      _checkLustreStatsFiles()
      return
    else:
      _checkLustreStatsFiles()
        
    # reset check counter
    num_reads = 0

  if not enabled:
    return

  collectd.debug("[Lustre Plugin] Collect for %d file systems" % (len(fsInfo) / FS_ENTRIES),)

  # get time stamp for all lustre metric values that we read
  timestamp = time.time()

  # iterate over file system info list in steps of FS_ENTRIES (as we have FS_ENTRIES entries per file system)
  #self.log.debug("[LustreCollector] %d, %d", len(self.fsInfo)-1, FS_ENTRIES)
  for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
    # skip disabled file systems
    if fsInfo[ idx + POS_ENABLED ] == False:
      continue

    statsFile = fsInfo[ idx ]
    if not statsFile:
      continue

    #self.log.debug("[LustreCollector] Collect from lustre %s (idx: %d), %d metrics", fs, idx, len(self.fsInfo[idx+2]))
    try:
      f = open( statsFile, "r" )
      finput = f.read()
      f.close()
    except IOError as ioe:
      collectd.error("[Lustre Plugin] Cannot read stats file: %s (%s)" % (statsFile, repr(ioe)))
    else:
      # parse the data into dictionary (key is metric name, value is metric value)
      lustrestat = _parseLustreStats( finput )

      # publish the metrics
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
    
    _checkLustreStatsFiles()
        
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
          
