#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <stdbool.h>
#include <string.h>

#include <likwid.h>

#ifdef TEST_LIWKID

#include <strings.h>

#define STATIC_ARRAY_SIZE(a) (sizeof(a) / sizeof(*(a)))

#define TIME_T_TO_CDTIME_T(t) t
/* Type for time as used by "utils_time.h" */
typedef uint64_t cdtime_t;

#define ERROR(...) plugin_log(0, __VA_ARGS__)
#define WARNING(...) plugin_log(0, __VA_ARGS__)
#define NOTICE(...) plugin_log(0, __VA_ARGS__)
#define INFO(...) plugin_log(0, __VA_ARGS__)
void plugin_log(int level, const char *format, ...) {
  char msg[1024];
  va_list ap;
  va_start(ap, format);
  vsnprintf(msg, sizeof(msg), format, ap);
  msg[sizeof(msg) - 1] = '\0';
  va_end(ap);
  fprintf(stderr, "%s\n", msg);
}

typedef void* notification_t;
typedef void* user_data_t;

#else

// headers required for collectd
#include "collectd.h"
#include "common.h" /* collectd auxiliary functions */
#include "plugin.h" /* plugin_register_*, plugin_dispatch_values */

#endif

#define PLUGIN_NAME "likwid"

static int  accessMode = 0; // direct access
static int  mTime = 15;       /**< Measurement time per group in seconds */
static cdtime_t mcdTime = 0;// TIME_T_TO_CDTIME_T(15);
static int  startSecond = 20;

static int likwid_verbose = 1;

static bool summarizeFlops = false;
static bool normalizeFlops = false;

static int numCPUs = 0;
static int* cpus = NULL;

static char* normalizedFlopsName = "flops_any";
static double* flopsValues = NULL; /**< storage to normalize FLOPS values flopsValues[cpu] */

static int numSockets = 0;
static int* socketInfoCores = NULL;

/*! \brief Metric type */
typedef struct metric_st {
  char* name;     /*!< metric name */
  uint8_t xFlops; /*!< if > 0, it is a FLOPS metric and the value is 
                       the multiplier for normalization */
  bool percpu;    /*!< true, if values are per CPU, otherwise per socket is assumed */
}metric_t;

/*! \brief Metric group type */
typedef struct metric_group_st {
  int id;            /*!< group ID */
  char* name;        /*!< group name */
  int numMetrics;    /*!< number of metrics in this group */
  metric_t *metrics; /*!< metrics in this group */
}metric_group_t;

static int numGroups = 0;
static metric_group_t* metricGroups = NULL;

/**< array of per socket metric names */
static int numSocketMetrics = 0;
static char** perSocketMetrics = NULL;

static char* mystrdup(const char *s)
{
  size_t len = strlen (s) + 1;
  char *result = (char*) malloc (len);
  if (result == (char*) 0)
    return (char*) 0;
  return (char*) memcpy (result, s, len);
}

/*! brief Determines by metric name, whether this is a per CPU or per socket metric. 
The default is "per CPU" */
static bool _isMetricPerCPU(const char* metric)
{
  for(int i = 0; i < numSocketMetrics; i++) {
    if(0 == strncmp(perSocketMetrics[i], metric, 6)) {
      return false;
    }
  }

  return true;
}

void _setupGroups()
{
  if(NULL == metricGroups)
  {
    ERROR(PLUGIN_NAME "No metric groups allocated! Plugin not initialized?");
    return;
  }

  INFO(PLUGIN_NAME ": Setup metric groups");
 
  int numFlopMetrics = 0;

  // set the group IDs and metric names
  for(int g = 0; g < numGroups; g++)
  {
    if(metricGroups[g].name != NULL )
    {
      int gid = perfmon_addEventSet(metricGroups[g].name);
      if(gid < 0)
      {
        metricGroups[g].id = -2;
        INFO(PLUGIN_NAME ": Failed to add group %s to LIKWID perfmon module (return code: %d)", metricGroups[g].name, gid);
      }
      else
      {
        // set the group ID
        metricGroups[g].id = gid;

        // get number of metrics for this group
        int numMetrics = perfmon_getNumberOfMetrics(gid);
        metricGroups[g].numMetrics = numMetrics;
        if(numMetrics == 0)
        {
          WARNING(PLUGIN_NAME ": Group %s has no metrics!", metricGroups[g].name);
          continue;
        }

        // allocate metric array
        metric_t* metrics = (metric_t*) malloc(numMetrics * sizeof(metric_t));
        if(NULL == metrics)
        {
          metricGroups[g].numMetrics = 0;
          metricGroups[g].id = -2;
          WARNING(PLUGIN_NAME ": Disable group %s as memory for metrics could not be allocated", metricGroups[g].name);
          continue;
        }

        // set the pointer to the allocated memory for metrics
        metricGroups[g].metrics = metrics;

        // set metric names and set initial values to -1
        for(int m = 0; m < numMetrics; m++)
        {
          metrics[m].name = perfmon_getMetricName(gid, m);

          // determine if metric is per CPU or per socket (by name)
          metrics[m].percpu = _isMetricPerCPU(metrics[m].name);

          // normalize flops, if enabled
          if( normalizeFlops && 0 == strncmp("flops", metrics[m].name, 5) )
          {
            numFlopMetrics++;

            // double precision to single precision = factor 2
            if(0 == strncmp("dp", metrics[m].name + 6, 5))
            {
              metrics[m].xFlops = 2;
            }
            // // avx to single precision = factor 4
            else if(0 == strncmp("avx", metrics[m].name + 6, 5))
            {
              metrics[m].xFlops = 4;
            }
            else // assume single precision otherwise
            {
              metrics[m].xFlops = 1;
            }
          }
          else
          {
            metrics[m].xFlops = 0;
          }
        } // END for metrics
      }
    }
    else
    {
      // set group ID to invalid
      metricGroups[g].id = -1;
    }
  } // END: for groups

  // check if FLOPS have to be aggregated (if more than one FLOP metric is collected),
  // which requires to allocate memory for each metric per core
  if (numFlopMetrics > 1) {
    INFO(PLUGIN_NAME ": Different FLOPS are aggregated and normalized.");
    summarizeFlops = true;

    flopsValues = (double*)malloc(numCPUs * sizeof(double));
    if (flopsValues) {
      // initialize with -1 (invalid value)
      memset(flopsValues, -1.0, numCPUs * sizeof(double));
    } else {
      WARNING(PLUGIN_NAME ": Could not allocate memory for normalization of FLOPS. Disable summarization of FLOPS.");
      summarizeFlops = false;
    }
  }

  // no need to handle different FLOPS in the same metric group, as this could
  // be handled directly in the Likwid metric group files
}

static int _init_likwid(void)
{
  perfmon_setVerbosity(likwid_verbose);
  
  topology_init();
  numa_init();
  affinity_init();
  //timer_init();
  HPMmode(accessMode);
  
  double timer = 0.0;
  CpuInfo_t cpuinfo = get_cpuInfo();
  CpuTopology_t cputopo = get_cpuTopology();
  numCPUs = cputopo->activeHWThreads;
  cpus = malloc(numCPUs * sizeof(int));
  if(!cpus)
  {   
      affinity_finalize();
      numa_finalize();
      topology_finalize();
      return 1;
  }

  int c = 0;
  for(int i = 0; i < cputopo->numHWThreads; i++)
  {   
      if (cputopo->threadPool[i].inCpuSet)
      {   
          cpus[c] = cputopo->threadPool[i].apicId;
          c++;
      }
  }

  // get socket information
  numSockets = cputopo->numSockets;
  uint32_t coresPerSocket = cputopo->numCoresPerSocket;
  socketInfoCores = malloc(numSockets*sizeof(int));
  if(NULL == socketInfoCores)
  {
    ERROR(PLUGIN_NAME ": Memory for socket information could not be allocated!");
    return 1;
  }
  for(int s = 0; s < numSockets; s++)
  {
    socketInfoCores[s] = s*coresPerSocket;
    INFO(PLUGIN_NAME ": Collecting per socket metrics for core: %d", socketInfoCores[s]);
  }

  NumaTopology_t numa = get_numaTopology();
  AffinityDomains_t affi = get_affinityDomains();
  //timer = timer_getCpuClock();
  perfmon_init(numCPUs, cpus);

  return 0;
}

static void _resetCounters(void)
{
  INFO(PLUGIN_NAME ": Set counters configuration!");

  for(int g = 0; g < numGroups; g++)
  {
    if(metricGroups[g].id < 0)
    {
      return;
    }

    perfmon_setCountersConfig(metricGroups[g].id);
  }
}

static const char* _getMeasurementName(metric_t *metric)
{
  if(metric->percpu) {
    return "likwid_cpu";
  }
  else
  {
    return "likwid_socket";
  }
}

/*! brief: cpu_idx is the index in the CPU array */
static bool _isSocketInfoCore(int cpu_idx)
{
  for(int s = 0; s < numSockets; s++) {
    if(cpu_idx == socketInfoCores[s]) {
      return true;
    }
  }
  return false;
}

#ifdef TEST_LIWKID



#else

// host "/" plugin ["-" plugin instance] "/" type ["-" type instance]
// e.g. taurusi2001/likwid_socket-0/cpi
// plugin field stores the measurement name (likwid_cpu or likwid_socket)
// the plugin instance stores the CPU ID
// the type field stores the metric name
// the type instance stores ???
static int _submit_value(const char* measurement, const char* metric, int cpu, double value, cdtime_t time) {
  value_list_t vl = VALUE_LIST_INIT;
  value_t v = {.gauge = value};
 
  vl.values = &v;
  vl.values_len = 1;
  
  vl.time = time;

  //const char* mname = getMeasurementName(metric);
  
  sstrncpy(vl.plugin, measurement, sizeof(vl.plugin));
  sstrncpy(vl.type, "likwid", sizeof(vl.type));
  sstrncpy(vl.type_instance, metric, sizeof(vl.type_instance));
  snprintf(vl.plugin_instance, sizeof(vl.plugin_instance), "%i", cpu);

  //INFO(PLUGIN_NAME ": dispatch: %s:%s(%d)=%lf", measurement, metric, cpu, value);

  plugin_dispatch_values(&vl);
}

static int likwid_plugin_read(void) {
  cdtime_t time = cdtime() + mcdTime * numGroups;
  
  //INFO(PLUGIN_NAME ": %s:%d (timestamp: %.3f)", __FUNCTION__, __LINE__, CDTIME_T_TO_DOUBLE(time));
  
  // read from likwid
  for(int g = 0; g < numGroups; g++) {
    int gid = metricGroups[g].id;
    if(gid < 0) {
      INFO(PLUGIN_NAME ": No eventset specified for group %s", metricGroups[g].name);
      continue;
    }

    if(0 != perfmon_setupCounters(gid)) {
      INFO(PLUGIN_NAME ": Could not setup counters for group %s", metricGroups[g].name);
      continue;
    }

    // measure counters for setup group
    perfmon_startCounters();
    sleep(mTime);
    perfmon_stopCounters();

    //int nmetrics = perfmon_getNumberOfMetrics(gid);
    int nmetrics = metricGroups[g].numMetrics;
    
    //INFO(PLUGIN_NAME ": Measured %d metrics for %d CPUs for group %s (%d sec)", nmetrics, numCPUs, metricGroups[g].name, mTime);

    // for all active hardware threads
    for(int c = 0; c < numCPUs; c++) {
      // for all metrics in the group
      for(int m = 0; m < nmetrics; m++) {
        double metricValue = perfmon_getLastMetric(gid, m, c);
        metric_t *metric = &(metricGroups[g].metrics[m]);

        //INFO(PLUGIN_NAME ": %lu - %s(%d):%lf", CDTIME_T_TO_TIME_T(time), metric->name, cpus[c], metricValue);

        char* metricName = metric->name; 
  
        //REMOVE: check that we write the value for the correct metric
        if ( 0 != strcmp(metricName, perfmon_getMetricName(gid, m))) {
          WARNING(PLUGIN_NAME ": Something went wrong!!!");
        }

        // skip cores that do not provide values for per socket metrics
        if (!metric->percpu && !_isSocketInfoCore(c)) {
          continue;
        }

        // special handling for FLOPS metrics
        if (metric->xFlops > 0) {
          // if user requested FLOPS normalization
          if(normalizeFlops) {
            // normalize FLOPS that are not already single precision (if requested)
            if(metric->xFlops > 1 && metricValue > 0) {
              metricValue *= metric->xFlops;
            }

            metricName = normalizedFlopsName;
          }

          // if there are at least two FLOPS metrics, aggregate their normalized values
          if (summarizeFlops) {
            //INFO(PLUGIN_NAME " FLOPS value set/add: %lu - %s(%d):%lf", CDTIME_T_TO_TIME_T(time), metric->name, cpus[c], metricValue);

            if (-1.0 == flopsValues[c]) {
              flopsValues[c] = metricValue;
            } else {
              flopsValues[c] += metricValue;
            }

            // do not submit yet
            continue;
          }
        }

        _submit_value(_getMeasurementName(metric), metricName, cpus[c], metricValue, time);
      }
    }
  }

  // submit metrics, if they have been summarized
  if(summarizeFlops)
  {
    for(int c = 0; c < numCPUs; c++) {
      _submit_value("likwid_cpu", normalizedFlopsName, cpus[c], flopsValues[c], time);

      // reset counter value
      flopsValues[c] = -1.0;
    }
  }

  return 0;
}
#endif

static int likwid_plugin_init(void)
{
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  // set the cdtime based on the measurement time per group
  mcdTime = TIME_T_TO_CDTIME_T(mTime);

  int ret = _init_likwid();
  
  _setupGroups();
  
  return ret;
}

/*! brief Resets the likwid group counters

Example notification on command line:
echo "PUTNOTIF severity=okay time=$(date +%s) message=resetLikwidCounters" |   socat - UNIX-CLIENT:$HOME/sw/collectd/collectd-unixsock
 */
static int likwid_plugin_notify(const notification_t *type, user_data_t *usr )
{
  _resetCounters();
}

static int likwid_plugin_finalize( void )
{
  INFO (PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  //perfmon_finalize(); // segfault
  affinity_finalize();
  numa_finalize();
  topology_finalize();

  // free memory where CPU IDs are stored
  //INFO(PLUGIN_NAME ": free allocated memory");
  if(NULL != cpus)
  {
    free(cpus);
  }

  if(NULL != metricGroups)
  {
    for(int i = 0; i < numGroups; i++)
    {
      // memory for group names have been allocated with strdup
      if(NULL != metricGroups[i].name)
      {
        free(metricGroups[i].name);
      }
    }
    free(metricGroups);

    if(flopsValues) {
      free(flopsValues);
    }
  }

  return 0;
}

static const char *config_keys[] =
{
  "NormalizeFlops",
  "AccessMode",
  "Mtime",
  "Groups",
  "PerSocketMetrics",
  "Verbose"
};
static int config_keys_num = STATIC_ARRAY_SIZE(config_keys);

static int likwid_plugin_config (const char *key, const char *value)
{
  INFO (PLUGIN_NAME " config: %s := %s", key, value);

  // use comma to separate metrics and metric groups
  // collectd converts commas in 'value' to spaces
  static char separator = ',';
  
  if (strcasecmp(key, "NormalizeFlops") == 0)
  {
    //normalizeFlops = IS_TRUE(value);
    normalizeFlops = true;
    normalizedFlopsName = mystrdup(value);
  }
  
  if (strcasecmp(key, "AccessMode") == 0)
  {
    accessMode = atoi(value);
  }
  
  if (strcasecmp(key, "Mtime") == 0)
  {
    mTime = atoi(value);
    //mcdTime = TIME_T_TO_CDTIME_T(mTime);
  }
  
  if (strcasecmp(key, "Verbose") == 0)
  {
    likwid_verbose = atoi(value);
  }
  
  if (strcasecmp(key, "Groups") == 0) {
    // using separate config lines would not allows us to allocate the metric,
    // group array, because the number of metrics was unknown
    
    // count number of groups
    numGroups = 1;
    int i = 0;
    while (value[i] != '\0') 
    { 
      if (value[i] == separator) 
      {
        numGroups++;
      }
      i++;
    }
    
    // allocate metric group array
    metricGroups = (metric_group_t*)malloc(numGroups * sizeof(metric_group_t));
    if(NULL == metricGroups)
    {
      ERROR(PLUGIN_NAME " Could not allocate memory for metric groups: %s", value);
      return 1; // config failed
    }

    // inialize metric groups
    for(int i = 0; i < numGroups; i++)
    {
      metricGroups[i].id = -1;
      metricGroups[i].name = NULL;
      metricGroups[i].numMetrics = 0;
      metricGroups[i].metrics = NULL;
    }
    
    i = 0;
    char *grp_ptr;
    char* myvalue = mystrdup(value); // need a copy as strtok modifies the first argument
    grp_ptr = strtok(myvalue, &separator);
    while( grp_ptr != NULL )
    {
      // save group name
      metricGroups[i].name = mystrdup(grp_ptr);
      INFO(PLUGIN_NAME " Found group: %s", grp_ptr);
      
      // get next group
      grp_ptr = strtok(NULL, &separator);
      
      i++;
    }
  }

  if (strcasecmp(key, "PerSocketMetrics") == 0) {
    // count number of per socket metrics
    numSocketMetrics = 1;
    int i = 0;
    while (value[i] != '\0') 
    { 
      if (value[i] == separator) 
      {
        numSocketMetrics++;
      }
      i++;
    }
    
    // allocate metric group array
    perSocketMetrics = (char**)malloc(numSocketMetrics * sizeof(char*));
    if(NULL == perSocketMetrics)
    {
      ERROR(PLUGIN_NAME " Could not allocate memory for per socket metrics: %s", value);
      numSocketMetrics = 0;
      return 1; // config failed
    }

    // tokenize the string by separator
    i = 0;
    char* myvalue = mystrdup(value); // need a copy as strtok modifies the first argument
    char *metric_ptr = strtok(myvalue, &separator);
    while( metric_ptr != NULL )
    {
      // save metric name
      perSocketMetrics[i] = mystrdup(metric_ptr);
      INFO(PLUGIN_NAME " Found per socket metric: %s", metric_ptr);
      
      // get next group
      metric_ptr = strtok(NULL, &separator);
      
      i++;
    }
  }
  
  return 0;
}

#ifndef TEST_LIWKID

/*
 * This function is called after loading the plugin to register it with collectd.
 */
void module_register(void) {
  plugin_register_config (PLUGIN_NAME, likwid_plugin_config, config_keys, config_keys_num);
  plugin_register_read(PLUGIN_NAME, likwid_plugin_read);
  plugin_register_init(PLUGIN_NAME, likwid_plugin_init);
  plugin_register_shutdown(PLUGIN_NAME, likwid_plugin_finalize);
  plugin_register_notification(PLUGIN_NAME, likwid_plugin_notify, /* user data = */ NULL);
  return;
}

#else

int main(int argc, char *argv[]) {
  // assume first argument to be the event group
  if( argc > 1 ) {
    for(int i = 1; i < argc; i++)
    {
      if (strncmp(argv[i], "-v", 2) == 0){
        likwid_verbose = atoi(argv[i]+2);
        fprintf(stderr, "Set LIKWID verbose level to %d\n", likwid_verbose);
      }else if (strncmp(argv[i], "-g", 2) == 0){
        fprintf(stderr, "Use group(s) %s\n", argv[i]+2);
        likwid_plugin_config ("Groups", argv[i]+2);
      }
    }
  }
  else
  {
    likwid_plugin_config ("Groups", "BRANCH");
  }
  
  likwid_plugin_config ("PerSocketMetrics", "mem_bw,rapl_power");

  // initialize LIKWID
  _init_likwid();

  _setupGroups();

  // finalize LIKWID
  likwid_plugin_finalize();

  return 0;
}

#endif
