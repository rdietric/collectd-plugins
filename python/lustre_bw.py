#!/usr/bin/python3
# coding=utf-8

"""
Collect data from Lustre file system

Author: Robert Dietrich (robert.dietrich@tu-dresden.de)

#### Dependencies

 * [subprocess](http://docs.python.org/library/subprocess.html)
"""

import time
import os
import sys
import subprocess

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

# lustre stats files are located depending on the lustre version
DEFAULT_LUSTRE_SEARCH_PATHS=['/sys/kernel/debug/lustre/llite/','/proc/fs/lustre/llite/']
### END: constants ###

### global variables ###
enabled = False

# comma separated list of Lustre file system instance paths (where stats file is located)
lustrePaths = None

# array of <fs name>:<relative mount subdirectory> (via configuration)
fsNameAndMountList = []

# time stamp of previous value dispatch
timePrev = 0

# file systems info array: 
# 3 entries per file system (full file system path, file system name, dict of last metrics values)
fsInfo = []
        
numReads = 0
checkSourcesInterval = 0 # number of intervals/reads after re-checking available file systems (default is off: 0)
### END: global variables ###

# "lfs getname": 
# scratch2-ffff984743280800 /lustre/scratch2
# highiops-ffff9847f44be000 /lustre/ssd
# scratch2-ffff98475d550000 /lustre/scratch2/ws
def _getMatchingInstances():
  cmd = 'lfs getname'

  try:
    status, result = subprocess.getstatusoutput(cmd)
  except Exception as ex:
    collectd.info("lustre plugin: Error launching '%s': %s" % (cmd, repr(ex)))
    return []

  # a zero status means without errors, 13 means permission denied (maybe only for some mounts)
  if status != 0 and status != 13:
    collectd.info("lustre plugin: Get lustre mount points failed (status: %s): %s" % (status, result))

  fs_name_mount_map = {}
  fs_name_instance_map = {}

  for line in result.split('\n'):
    # skip empty and invalid lines
    if line == "" or "Permission denied" in line:
      continue

    # split on whitespace to [file system instance, mount point]
    larray = line.split()

    if len(larray) < 2:
      collectd.info("lustre plugin: No mapping between mount and lustre instance possible!")
      continue

    if len(larray) > 2:
      collectd.info("lustre plugin: Mapping array of length %d." % (len(larray),))

    fs_instance = larray[0]
    fs_mount = larray[1]

    # if configuration provides file system names together with relative mount points
    if len(fsNameAndMountList) > 0:
      # for all configuration provided file system mounts
      for fsNameMount in fsNameAndMountList:
        conf_fsname, conf_mount = fsNameMount.split(":", 1)

        # allow asterix to search for all file systems
        if conf_fsname == '*':
          conf_fsname = ''

        if conf_fsname in fs_instance and fs_mount.endswith(conf_mount):
          if conf_fsname == '':
            conf_fsname = fs_instance.split("-", 1)[0]

          fs_name_mount_map[conf_fsname] = fs_mount
          fs_name_instance_map[conf_fsname] = fs_instance
    else:
      # we assume the the root mounts (shortest mount points per file system name are relevant)
      # get name (first part of lustre instance)
      fsname = fs_instance.split("-")[0]

      # if new file system name is not yet in dict or its mount is shorter
      if fsname not in fs_name_mount_map or len(fs_mount) < len(fs_name_mount_map[fsname]):
        fs_name_mount_map[fsname] = fs_mount
        fs_name_instance_map[fsname] = fs_instance

  if len(fs_name_mount_map) == 0:
    collectd.info("lustre plugin: No relevant file system mounts found!")
  else:
    for fs_name in fs_name_mount_map:
      collectd.info("lustre plugin: Using mount point %s for file system %s" % (fs_name_mount_map[fs_name], fs_name))

  return fs_name_instance_map.values()

# Return an array of lustre instance paths (either from config file or by searching in DEFAULT_LUSTRE_SEARCH_PATHS)
def _getLustreInstancePaths():
  if lustrePaths != None:
    collectd.debug( "lustre plugin: Use lustre paths %s from config file" % (lustrePaths,))
    return lustrePaths.split(',')
  else:
    fsArray = []
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
        collectd.info("lustre plugin: %s error launching: %s; skipping" % (repr(e), cmd))
        return []
      else:
        stdout= stdout.decode('utf-8')

      if stdout == '':
        collectd.info("lustre plugin: No file systems found: %s" % (cmd,))
        return []

      #collectd.info("lustre plugin: Found Lustre instance paths: %s" % (stdout,))
      
      fsArray = stdout.split('\n')
      fsArray.remove("") # remove empty string

    return fsArray

def _setupLustreFiles():
  global fsInfo
  fsInfo = []

  relevant_instances = _getMatchingInstances()
  
  for fsPath in _getLustreInstancePaths():
    if not fsPath:
      continue

    p_start = fsPath.rfind('/')
    p_end   = fsPath.rfind('-')

    # no '/' found
    if p_start == -1:
      collectd.info("lustre plugin: no start slash")
      continue

    # no '-' found
    if p_end == -1:
      p_end = fsPath.len()

    fs_instance = fsPath[(p_start + 1):]

    # setup only relevant mounts
    if fs_instance not in relevant_instances:
      continue

    collectd.info("lustre plugin: Collect data for '%s' from '%s'" % (fsPath[p_start+1:p_end],fsPath))

    fsInfo.append( fsPath + '/stats' ) # full path to the Lustre stats file
    fsInfo.append( fsPath[ p_start + 1 : p_end ] ) # name of file system, e.g. scratch
    fsInfo.append( False ) # first, disable the file system

    # append array entry for lustre offset dictionary
    fsInfo.append( {} )

  #collectd.info("lustre plugin: Found %d file systems" % (len(fsInfo) / FS_ENTRIES,))

  # gather first/prev values
  if len(fsInfo) > 0:
    _setPrevValues()
  else:
    global enabled
    enabled = False
    collectd.info("lustre plugin: No file systems found, Disable plugin for %d reads." % (checkSourcesInterval,) )

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
      collectd.info( "lustre plugin: Cannot read from %s (%s)" % (statsFile, repr(ioe),))
      fsInfo[ idx + POS_ENABLED ] = False
      continue
    else:
      enabled = True
      fsInfo[ idx + POS_ENABLED ] = True
      stats_offsets = _parseLustreStats( finput )
      fsInfo[ idx + POS_PREV_DATA ].update( stats_offsets )

    # store timestamp of previous data
    global timePrev
    timePrev = time.time()
        
# check if there are file systems available, which are not monitored yet
def _haveNewFS():        
  for fsInstanceNew in _getMatchingInstances():
    newFS=True
    # mark the FS as not new, if it is in the current list
    for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):      
      if fsInstanceNew in fsInfo[ idx ]:
        newFS = False
        break
    
    if newFS:
      collectd.info("lustre plugin: Found new Lustre instance %s!" % (fsInstanceNew,))
      return True
    
  return False

# Check for the existence of the stats files and enable/disable.
def _checkLustreStatsFiles():
  # iterate over file system info list in steps of FS_ENTRIES
  for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
    # disable file system, if stats file does not exist
    if not os.path.isfile(fsInfo[idx]):
      fsInfo[idx + POS_ENABLED] = False
      collectd.warning("lustre plugin: Disable reading from %s (file not found)." % (fsInfo[idx],))
    elif fsInfo[idx + POS_ENABLED] == False:
      fsInfo[idx + POS_ENABLED] = True
      collectd.info("lustre plugin: Enable reading from %s." % (fsInfo[idx],))

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

    interval = timestamp - timePrev

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
        # TODO: change to derive type?
        vl = collectd.Values(type='gauge')
        vl.plugin='lustre_' + fsname
        vl.values = [float(currValue) / float(interval)]
        vl.time = timestamp
        vl.type_instance = metric
        vl.dispatch()
      else:
        collectd.debug("lustre plugin: %d: bandwidth < 0 (current: %f, previous available? %s" % (timestamp, lustreMetrics[ metric ], metric in previous))

def lustre_plugin_config(config):
  if config.values[0] == 'lustre_bw':
    collectd.info("lustre plugin: Get configuration")
    for value in config.children:
      if value.key == 'path':
        global lustrePaths
        lustrePaths = value.values[0]
        collectd.info("lustre plugin: Paths to lustre file system instance stat files: %s" % (lustrePaths,))
      elif value.key == 'fsname_and_mount':
        global fsNameAndMountList
        # assume that the values are: <file system name>:<relative mount directory>
        fsNameAndMountList.append(value.values[0])
      elif value.key == 'recheck_limit':
        global checkSourcesInterval
        checkSourcesInterval = int(value.values[0])
        if checkSourcesInterval > 0:
          collectd.info("lustre plugin: Check for available lustre file systems every %d collects" % (checkSourcesInterval,))
      

def lustre_plugin_initialize():
  collectd.debug("lustre plugin: Initialize ...")

  #collectd.info("Python version: %d.%d.%d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))

  # setup lustre file paths and initialize previous values
  _setupLustreFiles()

  _checkLustreStatsFiles()


"""
brief Read send and receive counters from Infiniband devices
"""
def lustre_plugin_read(data=None):
  #self.log.debug( "Collect %d ? %d", num_reads, recheck_limit)

  # check for available file systems every #recheck_limit reads
  global numReads
  numReads += 1
  
  # check for available file systems
  if numReads == checkSourcesInterval:
    if _haveNewFS():
      _setupLustreFiles()
      _checkLustreStatsFiles()
      return
    else:
      _checkLustreStatsFiles()
        
    # reset check counter
    numReads = 0

  if not enabled:
    return

  #collectd.debug("lustre plugin: Collect for %d file systems" % (len(fsInfo) / FS_ENTRIES),)

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
      collectd.error("lustre plugin: Cannot read %s (%s). Disable reading!" % (statsFile, repr(ioe)))
      fsInfo[ idx + POS_ENABLED ] = False
    else:
      # parse the data into dictionary (key is metric name, value is metric value)
      lustrestat = _parseLustreStats( finput )

      # publish the metrics
      _publishLustreMetrics( idx, lustrestat, timestamp )

  global timePrev
  timePrev = timestamp

# paste on command line
#echo "PUTNOTIF severity=okay time=$(date +%s) message=hello" | socat - UNIX-CLIENT:/home/rdietric/sw/collectd/5.8.0/var/run/collectd-unixsock
def lustre_plugin_notify(notification, data=None):
  #collectd.info("lustre plugin: Notification: %s" % (str(notification),))
  
  # for severity okay (4)
  if notification.severity == 4 and notification.message == "check":
    collectd.info("lustre plugin: Check Lustre files ...")
    if _haveNewFS():
      _setupLustreFiles()
    
    _checkLustreStatsFiles()
        
    # reset check counter
    global numReads
    numReads = 0


if __name__ != "__main__":
  # when running inside plugin register each callback
  collectd.register_config(lustre_plugin_config)
  collectd.register_init(lustre_plugin_initialize)
  collectd.register_read(lustre_plugin_read)
  collectd.register_notification(lustre_plugin_notify)
else:
  # outside plugin just collect the info

  ### manual configuration ###
  # for all file systems, where mount poinst end with /ws
  fsNameAndMountList.append("*:/ws")
  # test re-check
  checkSourcesInterval = 5

  # initialize plugin and read once
  lustre_plugin_initialize()
  lustre_plugin_read()
  
  # start a read loop, if no arguments are given
  if len(sys.argv) < 2:
    while True:
        time.sleep(10)
        lustre_plugin_read()
          
