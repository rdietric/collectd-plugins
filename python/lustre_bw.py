#!/usr/bin/python3
# coding=utf-8

"""
Collect data from Lustre file systems.

Author: Robert Dietrich (robert.dietrich@tu-dresden.de)

Dependencies:
[subprocess](http://docs.python.org/library/subprocess.html)
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
# number of list entries per Lustre instance and positions
FS_ENTRIES = 3
POS_STATS_FILE = 0 # not used
POS_FSNAME = 1
POS_PREV_DATA = 2

# Lustre meta data operations
KEY_MAPPING = [
  'open',
  'close',
  'fsync',
  'create',
  'seek'
]

# Lustre stats files are located depending on the Lustre version
# make sure that the paths end with a slash
DEFAULT_LUSTRE_SEARCH_PATHS = [
  '/sys/kernel/debug/lustre/llite/',
  '/proc/fs/lustre/llite/'
]
### END: constants ###

### global variables ###
# controls whether plugin is enabled or disabled
enabled = False

# Lustre file system instance paths (where stats file is located), set via conf
confLustreInstancesPath = None

# Lustre instances (fsname-MAGICNUM), set via conf
confLustreInstances = None

# array of <fs name>:<relative mount subdirectory> (via configuration)
confFsNameMountList = None

# path where Lustre instances with stats file are located
lustrePath = None

# list of monitored Lustre instances
lustreInstances = None

# time stamp of previous value dispatch
timePrev = 0

# Lustre instances information list: 
# FS_ENTRIES entries per instance (path to stats file, file system name, 
# dict of last metrics values)
fsInfo = []
        
numReads = 0
checkSourcesInterval = 0 # number of intervals/reads after re-checking available file systems (default is off: 0)
### END: global variables ###

"""
Check if one of the default search paths exists and set it as Lustre instances
path. This functions assumes that only one path exists and takes the first path
in the list that exists.
"""
def _setLustrePath():
  for searchPath in DEFAULT_LUSTRE_SEARCH_PATHS:
    if os.path.exists(searchPath):
      global lustrePath
      lustrePath = searchPath
      collectd.info("lustre plugin: Use Lustre path '%s'" % (searchPath,) )
      return

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
    if confFsNameMountList is not None:
      # for all configuration provided file system mounts
      for fsNameMount in confFsNameMountList:
        conf_fsname, conf_mount = fsNameMount.split(":", 1)

        # allow asterix to search for all file systems
        if conf_fsname == '*':
          conf_fsname = '' # empty string is part of every string

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

"""
Check if there is only one instance per file system name.
Return True, if there are multiple instances per file system. Sets also the 
global variable haveMultipleInstancesPerFS.
"""
def _haveMultipleFsInstances(instances):
  fsNameArray = []
  for fsInstance in instances:
    if fsInstance is None:
      continue
    
    fsName = fsInstance.split('-',1)[0]
    if fsName in fsNameArray:
      return True
    fsNameArray.append(fsName)

  return False

"""
Return a list of Lustre instance paths. Requires Lustre path to be set. 
"""
def _getAllLustreInstances():
  if lustrePath is None:
    collectd.error("lustre plugin: Lustre path is not set!")
    return []
  # find file systems
  #cmd = 'find ' + lustrePath + '* -maxdepth 0 -type d 2>/dev/null'
  #cmd = 'ls -d ' + lustrePath + '*' # list content of lustrePath with full path
  cmd = 'ls ' + lustrePath # list content of lustrePath
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
  lustreInstances = stdout.split('\n')
  lustreInstances.remove("") # remove empty lines
  return lustreInstances

"""
Determines the Lustre intances that should be monitored. 
"""
def _setLustreInstances():
  global lustreInstances

  # check whether specific Lustre instances should be monitored
  if confLustreInstancesPath is not None and confLustreInstances is not None:
    if not confLustreInstancesPath.endswith('/'):
      confLustreInstancesPath + "/"

    for instance in confLustreInstances:
      collectd.info( "lustre plugin: Monitor %s%s (see conf file)" % (confLustreInstancesPath, instance))

    lustreInstances = confLustreInstances
  else:
    lustreInstances = _getAllLustreInstances()
  
    # if we have multiple Lustre instances per file system, determine the 
    # instances that should be monitored
    if _haveMultipleFsInstances(lustreInstances):
      lustreInstances = _getMatchingInstances()

"""
Setup the Lustre instance paths, where stats files are located.
"""
def _setupLustreFiles():
  global fsInfo
  fsInfo = []

  for fsInstance in lustreInstances:    
    fsName = fsInstance.split('-',1)[0]

    collectd.info("lustre plugin: Collect data for '%s'" % (fsInstance,))

    fsInfo.append( lustrePath + fsInstance + '/stats' ) # full path to the Lustre stats file
    fsInfo.append( fsName ) # name of file system, e.g. scratch

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

"""
Remove Lustre instance from global list via index to the stats file path.
"""
def _removeInstances(idxList):
  for idx in idxList:
    del fsInfo[idx:idx+FS_ENTRIES]
        
"""
Set initial values for each Lustre intsance to determine difference (increase).
"""
def _setPrevValues():
  global enabled

  deleteList = None
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
      
      # add Lustre instance to delete list
      if deleteList is None:
        deleteList = [idx]
      else:
        deleteList.append(idx)
      continue
    else:
      enabled = True
      stats_offsets = _parseLustreStats( finput )
      fsInfo[ idx + POS_PREV_DATA ].update( stats_offsets )

  if deleteList is not None:
    _removeInstances(deleteList)

  # store timestamp of previous data
  if enabled:
    global timePrev
    timePrev = time.time()
        
"""
Check if there are file system instances available, which are not monitored yet.
Return True, if a new file system instance has been found.
"""
def _haveNewFS():
  for instance in lustreInstances:
    newFS=True
    # mark the FS instance as not new, if it is in the current list
    for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
      # fsInfo provides the full paths with the instances at the end
      if instance in fsInfo[ idx ]:
        newFS = False
        break
    
    if newFS:
      collectd.info("lustre plugin: Found new Lustre instance %s!" % (instance,))
      return True
    
  return False

""" 
Check for the existence of the stats files and clean up global list of Lustre 
instance data. Delete a Lustre instance instead of disable it, when mounting 
again the instance magic ID probably changes and we want to avoid long lists
with many disabled instances that will never get enabled again.
"""
def _checkLustreStatsFiles():
  deleteList = None
  # iterate over file system info list in steps of FS_ENTRIES
  for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
    # disable file system, if stats file does not exist
    if not os.path.isfile(fsInfo[idx]):
      collectd.warning("lustre plugin: Stop reading from %s (file not found)." % (fsInfo[idx],))
      if deleteList is None:
        deleteList = [idx]
      else:
        deleteList.append(idx)

  if deleteList is not None:
    _removeInstances(deleteList)

"""
Parse the lustre stats file.
Return dictionary with metric names (key) and value (value)
"""
def _parseLustreStats(finput):
  lustrestat = {}
  try:
    for line in filter( None, finput.split('\n') ):
      linelist = line.split() #re.split( "\s+", line ) #split is faster than re.split
      if linelist[0] == "read_bytes":
        lustrestat["read_requests"] = float(linelist[1]) 
        lustrestat["read_bw"] = float(linelist[6])
      elif linelist[0] == "write_bytes":
        lustrestat["write_requests"] = float(linelist[1]) 
        lustrestat["write_bw"] = float(linelist[6])
      elif linelist[0] in KEY_MAPPING:
        lustrestat[linelist[0]] = float(linelist[1])
  except IndexError:
    collectd.error("lustre plugin: Index error in parsing lustre stats")

  return lustrestat

"""
Dispatch acquired metrics.
"""
def _dispatchLustreMetrics(fsIdx, lustreMetrics, timestamp): 
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

"""
Check for Lustre files. Return True, if a new file instance was found.
"""
def _run_check():
  ret = False

  _setLustrePath()
  _setLustreInstances()

  if _haveNewFS(): # this should happen very rarely
    _setupLustreFiles()
    ret = True

  _checkLustreStatsFiles()

  # reset check counter
  global numReads
  numReads = 0

  return ret

"""
Collectd configuration callback
"""
def lustre_plugin_config(config):
  if config.values[0] == 'lustre_bw':
    collectd.info("lustre plugin: Get configuration")
    for value in config.children:
      if value.key == 'path':
        global confLustreInstancesPath
        confLustreInstancesPath = value.values[0]
        collectd.info("lustre plugin: Paths to Lustre file system instances: %s" % (confLustreInstancesPath,))
      elif value.key == 'instances':
        global confLustreInstances
        confLustreInstances = value.values[0].split(",")
      elif value.key == 'fsname_and_mount':
        global confFsNameMountList
        if confFsNameMountList is None:
          confFsNameMountList = []
        # assume that the values are: <file system name>:<relative mount directory>
        confFsNameMountList.append(value.values[0])
      elif value.key == 'recheck_limit':
        global checkSourcesInterval
        checkSourcesInterval = int(value.values[0])
        if checkSourcesInterval > 0:
          collectd.info("lustre plugin: Check for available Lustre file systems every %d reads" % (checkSourcesInterval,))
      
"""
Collectd plugin initialization callback.
"""
def lustre_plugin_initialize():
  collectd.debug("lustre plugin: Initialize ...")

  #collectd.info("Python version: %d.%d.%d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))

  # the order of the following function calls is important
  _setLustrePath()
  _setLustreInstances()
  _setupLustreFiles()
  _checkLustreStatsFiles()


"""
Read the Lustre stats files for all setup Lustre instances.
"""
def lustre_plugin_read(data=None):
  #self.log.debug( "Collect %d ? %d", num_reads, recheck_limit)

  # check for available file systems every #recheck_limit reads
  global numReads
  numReads += 1
  
  # check for available file systems
  if numReads == checkSourcesInterval:
    # check, if the Lustre setup changed
    if _run_check():
      return

  if not enabled:
    return

  #collectd.debug("lustre plugin: Collect for %d file systems" % (len(fsInfo) / FS_ENTRIES),)

  # get time stamp for all lustre metric values that we read
  timestamp = time.time()

  # iterate over file system info list in steps of FS_ENTRIES (as we have FS_ENTRIES entries per file system)
  #self.log.debug("[LustreCollector] %d, %d", len(self.fsInfo)-1, FS_ENTRIES)
  deleteList = None
  for idx in range( 0, len(fsInfo)-1, FS_ENTRIES):
    statsFile = fsInfo[ idx ]
    if not statsFile:
      continue

    #self.log.debug("[LustreCollector] Collect from lustre %s (idx: %d), %d metrics", fs, idx, len(self.fsInfo[idx+2]))
    try:
      f = open( statsFile, "r" )
      finput = f.read()
      f.close()
    except IOError as ioe:
      collectd.error("lustre plugin: Cannot read %s (%s). Stop reading!" % (statsFile, repr(ioe)))
      if deleteList is None:
        deleteList = [idx]
      else:
        deleteList.append(idx)
    else:
      # parse the data into dictionary (key is metric name, value is metric value)
      lustrestat = _parseLustreStats( finput )
      _dispatchLustreMetrics( idx, lustrestat, timestamp )

  if deleteList is not None:
    _removeInstances(deleteList)

  global timePrev
  timePrev = timestamp

"""
Handle notifications, e.g. trigger check and enable/disable reading.
To trigger this function, use the socket plugin and in a terminal: 
echo "PUTNOTIF severity=okay time=$(date +%s) message=hello" | socat - UNIX-CLIENT:collectdSocketFile.sock
"""
def lustre_plugin_notify(notification, data=None):
  #collectd.info("lustre plugin: Notification: %s" % (str(notification),))
  if notification.plugin is None or notification.plugin == "" or notification.plugin == "lustre_bw":
    global enabled
    if notification.message == "check":
      collectd.info("lustre plugin: Check Lustre files.")
      _run_check()
    elif notification.message == "disable":
      collectd.info("lustre plugin: Disable reading")
      enabled = False
    elif notification.message == "enable":
      collectd.info("lustre plugin: Enable reading")
      enabled = True
    elif notification.message == "unregister":
      collectd.info("lustre plugin: Unregister read callback ...")
      try:
        collectd.unregister_read(lustre_plugin_read)
      except:
        collectd.error("lustre plugin: Could not unregister read callback!")
    elif notification.message == "register":
      collectd.info("lustre plugin: Register read callback ...")
      try:
        collectd.register_read(lustre_plugin_read)
      except:
        collectd.error("lustre plugin: Could not register read callback!")

if __name__ != "__main__":
  # when running inside plugin register each callback
  collectd.register_config(lustre_plugin_config)
  collectd.register_init(lustre_plugin_initialize)
  collectd.register_read(lustre_plugin_read)
  collectd.register_notification(lustre_plugin_notify)
else:
  # outside plugin just collect the info

  ### manual configuration ###
  DEFAULT_LUSTRE_SEARCH_PATHS.append('/home/rdietric/svn_local/collectd-plugins/python/LUSTRE_PATH/')

  # for all file systems, where mount poinst end with /ws
  confFsNameMountList = ["*:/ws"]
  # test re-check
  checkSourcesInterval = 4

  # initialize plugin and read once
  lustre_plugin_initialize()
  lustre_plugin_read()
  
  # start a read loop, if no arguments are given
  if len(sys.argv) < 2:
    while True:
        time.sleep(10)
        lustre_plugin_read()
          
