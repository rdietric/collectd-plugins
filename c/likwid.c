#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <likwid.h>

#ifdef TEST_LIWKID
#include <inttypes.h>
#include <sys/time.h>
#include <time.h>

#define STATIC_ARRAY_SIZE(a) (sizeof(a) / sizeof(*(a)))

/********* Collectd time stuff ***********/
#define TIME_T_TO_CDTIME_T_STATIC(t) (((cdtime_t)(t)) << 30)
#define TIME_T_TO_CDTIME_T(t)                                                  \
  (cdtime_t) { TIME_T_TO_CDTIME_T_STATIC(t) }
#define NS_TO_CDTIME_T(ns)                                                     \
  (cdtime_t) {                                                                 \
    ((((cdtime_t)(ns)) / 1000000000) << 30) |                                  \
        ((((((cdtime_t)(ns)) % 1000000000) << 30) + 500000000) / 1000000000)   \
  }
#define TIMESPEC_TO_CDTIME_T(ts)                                               \
  NS_TO_CDTIME_T(1000000000ULL * (ts)->tv_sec + (ts)->tv_nsec)
/* Type for time as used by "utils_time.h" */
typedef uint64_t cdtime_t;
cdtime_t cdtime(void) /* {{{ */
{
  int status;
  struct timespec ts = {0, 0};

  status = clock_gettime(CLOCK_REALTIME, &ts);
  if (status != 0) {
    printf("cdtime: clock_gettime failed\n");
    return 0;
  }

  return TIMESPEC_TO_CDTIME_T(&ts);
} /* }}} cdtime_t cdtime */
  /********* END: Collectd time stuff ***********/

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

typedef void *notification_t;
typedef void *user_data_t;

#else

// headers required for collectd
#include "collectd.h"
#include "common.h" /* collectd auxiliary functions */
#include "plugin.h" /* plugin_register_*, plugin_dispatch_values */

#endif

#define PLUGIN_NAME "likwid"

static int accessMode = 0;   // direct access
static int mTime = 10;       /**< Measurement time per group in seconds */
static cdtime_t mcdTime = 0; // TIME_T_TO_CDTIME_T(15);
static int startSecond = 20;

static int likwid_verbose = 1;

static bool summarizeFlops = false;
static bool normalizeFlops = false;
static char *normalizedFlopsName = "flops_any";
static double *flopsValues =
    NULL; /**< storage to normalize FLOPS values flopsValues[cpu] */

/*! \brief Maximum values for metrics */
typedef struct {
  char *metricName; /*!< metric name */
  double maxValue;
} max_value_t;
static max_value_t *maxValues = NULL;
static int numMaxValues = 0;

static int numCPUs = 0;
static int *cpus = NULL;

static int numSockets = 0;
static int *socketInfoCores = NULL;

static bool plugin_disabled = false;

/*! \brief Metric type */
typedef struct metric_st {
  char *name;     /*!< metric name */
  uint8_t xFlops; /*!< if > 0, it is a FLOPS metric and the value is
                       the multiplier for normalization */
  bool percpu; /*!< true, if values are per CPU, otherwise per socket is assumed
                */
  double maxValue;
} metric_t;

/*! \brief Metric group type */
typedef struct metric_group_st {
  int id;            /*!< group ID */
  char *name;        /*!< group name */
  int numMetrics;    /*!< number of metrics in this group */
  metric_t *metrics; /*!< metrics in this group */
} metric_group_t;

static int numGroups = 0;
static metric_group_t *metricGroups = NULL;

/**< array of per socket metric names */
static int numSocketMetrics = 0;
static char **perSocketMetrics = NULL;

static char *mystrdup(const char *s) {
  size_t len = strlen(s) + 1;
  char *result = (char *)malloc(len);
  if (result == (char *)0)
    return (char *)0;
  return (char *)memcpy(result, s, len);
}

/*! brief Determines by metric name, whether this is a per CPU or per socket
metric. The default is "per CPU" */
static bool _isMetricPerCPU(const char *metric) {
  for (int i = 0; i < numSocketMetrics; i++) {
    if (0 == strncmp(perSocketMetrics[i], metric, 6)) {
      return false;
    }
  }

  return true;
}

void _setupGroups() {
  if (NULL == metricGroups) {
    ERROR(PLUGIN_NAME "No metric groups allocated! Plugin not initialized?");
    return;
  }

  INFO(PLUGIN_NAME ": Setup metric groups");

  int numFlopMetrics = 0;

  // set the group IDs and metric names
  for (int g = 0; g < numGroups; g++) {
    if (metricGroups[g].name != NULL) {
      int gid = perfmon_addEventSet(metricGroups[g].name);
      if (gid < 0) {
        metricGroups[g].id = -2;
        INFO(PLUGIN_NAME ": Failed to add group %s to LIKWID perfmon module "
                         "(return code: %d)",
             metricGroups[g].name, gid);
      } else {
        // set the group ID
        metricGroups[g].id = gid;

        // get number of metrics for this group
        int numMetrics = perfmon_getNumberOfMetrics(gid);
        metricGroups[g].numMetrics = numMetrics;
        if (numMetrics == 0) {
          WARNING(PLUGIN_NAME ": Group %s has no metrics!",
                  metricGroups[g].name);
          continue;
        }

        // allocate metric array
        metric_t *metrics = (metric_t *)malloc(numMetrics * sizeof(metric_t));
        if (NULL == metrics) {
          metricGroups[g].numMetrics = 0;
          metricGroups[g].id = -2;
          WARNING(
              PLUGIN_NAME
              ": Disable group %s as memory for metrics could not be allocated",
              metricGroups[g].name);
          continue;
        }

        // set the pointer to the allocated memory for metrics
        metricGroups[g].metrics = metrics;

        // set metric names and set initial values to -1
        for (int m = 0; m < numMetrics; m++) {
          metrics[m].name = perfmon_getMetricName(gid, m);

          // determine if metric is per CPU or per socket (by name)
          metrics[m].percpu = _isMetricPerCPU(metrics[m].name);

          // normalize flops, if enabled
          if (normalizeFlops && 0 == strncmp("flops", metrics[m].name, 5)) {
            numFlopMetrics++;

            // double precision to single precision = factor 2
            if (0 == strncmp("dp", metrics[m].name + 6, 5)) {
              metrics[m].xFlops = 2;
            }
            // // avx to single precision = factor 4
            else if (0 == strncmp("avx", metrics[m].name + 6, 5)) {
              metrics[m].xFlops = 4;
            } else // assume single precision otherwise
            {
              metrics[m].xFlops = 1;
            }
          } else {
            metrics[m].xFlops = 0;
          }

          // set maximum value of metric, if available
          metrics[m].maxValue = 0.0;
          /*for (int i = 0; i < numMaxValues; i++) {
            if (0 == strncmp(metrics[m].name, maxValues[i].metricName, strlen(maxValues[i].metricName))) {
              metrics[m].maxValue = maxValues[i].maxValue;
            }
          }*/
        } // END for metrics
      }
    } else {
      // set group ID to invalid
      metricGroups[g].id = -1;
    }
  } // END: for groups

  // check if FLOPS have to be aggregated (if more than one FLOP metric is
  // collected), which requires to allocate memory for each metric per core
  if (numFlopMetrics > 1) {
    INFO(PLUGIN_NAME ": Different FLOPS are aggregated and normalized.");
    summarizeFlops = true;

    flopsValues = (double *)malloc(numCPUs * sizeof(double));
    if (flopsValues) {
      // initialize with -1 (invalid value)
      memset(flopsValues, -1.0, numCPUs * sizeof(double));
    } else {
      WARNING(PLUGIN_NAME ": Could not allocate memory for normalization of "
                          "FLOPS. Disable summarization of FLOPS.");
      summarizeFlops = false;
    }
  }

  // no need to handle different FLOPS in the same metric group, as this could
  // be handled directly in the Likwid metric group files
}

static int _init_likwid(void) {
  perfmon_setVerbosity(likwid_verbose);

  topology_init();
  numa_init();
  affinity_init();
  // timer_init();
  HPMmode(accessMode);

  double timer = 0.0;
  CpuInfo_t cpuinfo = get_cpuInfo();
  CpuTopology_t cputopo = get_cpuTopology();
  numCPUs = cputopo->numHWThreads;
  cpus = malloc(numCPUs * sizeof(int));
  if (!cpus) {
    affinity_finalize();
    numa_finalize();
    topology_finalize();
    return 1;
  }

  for (int i = 0; i < cputopo->numHWThreads; i++) {
    cpus[i] = cputopo->threadPool[i].apicId;
  }

  // get socket information
  numSockets = cputopo->numSockets;
  socketInfoCores = malloc(numSockets * sizeof(int));
  if (NULL == socketInfoCores) {
    ERROR(PLUGIN_NAME
          ": Memory for socket information could not be allocated!");
    return 1;
  }
  for (int s = 0; s < numSockets; s++) {
    socketInfoCores[s] =
        s * cputopo->numCoresPerSocket * cputopo->numThreadsPerCore;
    INFO(PLUGIN_NAME ": Collecting per socket metrics for core: %d",
         socketInfoCores[s]);
  }

  NumaTopology_t numa = get_numaTopology();
  AffinityDomains_t affi = get_affinityDomains();
  // timer = timer_getCpuClock();
  perfmon_init(numCPUs, cpus);

  return 0;
}

#ifndef TEST_LIWKID
static void _resetCounters(void) {
  INFO(PLUGIN_NAME ": (Re)set counters configuration for %d groups!",
       numGroups);

  for (int g = 0; g < numGroups; g++) {
    if (metricGroups[g].id < 0) {
      return;
    }

    perfmon_setCountersConfig(metricGroups[g].id);
  }
}
#endif

static const char *_getMeasurementName(metric_t *metric) {
  if (metric->percpu) {
    return "likwid_cpu";
  } else {
    return "likwid_socket";
  }
}

/*! brief: cpu_idx is the index in the CPU array */
static bool _isSocketInfoCore(int cpu_idx) {
  for (int s = 0; s < numSockets; s++) {
    if (cpu_idx == socketInfoCores[s]) {
      return true;
    }
  }
  return false;
}

#ifdef TEST_LIWKID

static int _submit_value(const char *measurement, const char *metric, int cpu,
                         double value, cdtime_t time) {
  fprintf(stderr, "%d: %s - %s = %lf (%" PRIu64 ")\n", cpu, measurement, metric,
          value, time);
  return 0;
}

#else

// host "/" plugin ["-" plugin instance] "/" type ["-" type instance]
// e.g. taurusi2001/likwid_socket-0/cpi
// plugin field stores the measurement name (likwid_cpu or likwid_socket)
// the plugin instance stores the CPU ID
// the type field stores the metric name
// the type instance stores ???
static int _submit_value(const char *measurement, const char *metric, int cpu,
                         double value, cdtime_t time) {
  value_list_t vl = VALUE_LIST_INIT;
  value_t v = {.gauge = value};

  vl.values = &v;
  vl.values_len = 1;

  vl.time = time;

  // const char* mname = getMeasurementName(metric);

  sstrncpy(vl.plugin, measurement, sizeof(vl.plugin));
  sstrncpy(vl.type, "likwid", sizeof(vl.type));
  sstrncpy(vl.type_instance, metric, sizeof(vl.type_instance));
  snprintf(vl.plugin_instance, sizeof(vl.plugin_instance), "%i", cpu);

  // INFO(PLUGIN_NAME ": dispatch: %s:%s(%d)=%lf", measurement, metric, cpu,
  // value);

  plugin_dispatch_values(&vl);
}
#endif

static int likwid_plugin_read(void) {
  if (plugin_disabled) {
    return 0;
  }

  cdtime_t time = cdtime() + mcdTime * numGroups;

  // INFO(PLUGIN_NAME ": %s:%d (timestamp: %.3f)", __FUNCTION__, __LINE__,
  // CDTIME_T_TO_DOUBLE(time));

  // read from likwid
  for (int g = 0; g < numGroups; g++) {
    int gid = metricGroups[g].id;
    if (gid < 0) {
      INFO(PLUGIN_NAME ": No eventset specified for group %s",
           metricGroups[g].name);
      sleep(mTime);
      continue;
    }

    if (0 != perfmon_setupCounters(gid)) {
      INFO(PLUGIN_NAME ": Could not setup counters for group %s",
           metricGroups[g].name);
      continue;
    }

    // measure counters for setup group
    perfmon_startCounters();
    sleep(mTime);
    perfmon_stopCounters();

    // int nmetrics = perfmon_getNumberOfMetrics(gid);
    int nmetrics = metricGroups[g].numMetrics;

    // INFO(PLUGIN_NAME ": Measured %d metrics for %d CPUs for group %s (%d
    // sec)", nmetrics, numCPUs, metricGroups[g].name, mTime);

    // for all active hardware threads
    for (int c = 0; c < numCPUs; c++) {
      // for all metrics in the group
      for (int m = 0; m < nmetrics; m++) {
        double metricValue = perfmon_getLastMetric(gid, m, c);
        metric_t *metric = &(metricGroups[g].metrics[m]);

        // INFO(PLUGIN_NAME ": %lu - %s(%d):%lf", CDTIME_T_TO_TIME_T(time),
        // metric->name, cpus[c], metricValue);

        char *metricName = metric->name;

#ifdef DEBUG
        // REMOVE: check that we write the value for the correct metric
        if (0 != strcmp(metricName, perfmon_getMetricName(gid, m))) {
          WARNING(PLUGIN_NAME ": Something went wrong!!!");
        }
#endif

        // skip cores that do not provide values for per socket metrics
        if (!metric->percpu && !_isSocketInfoCore(c)) {
          continue;
        }

        /*if (metric->maxValue != 0.0 && metricValue > metric->maxValue) {
          INFO(PLUGIN_NAME ": Skipping outlier for %s: %.3lf", metricName,
               metricValue);
          continue;
        }*/

        // special handling for FLOPS metrics
        if (metric->xFlops > 0) {
          // if user requested FLOPS normalization
          if (normalizeFlops) {
            // normalize FLOPS that are not already single precision (if
            // requested)
            if (metric->xFlops > 1 && metricValue > 0) {
              metricValue *= metric->xFlops;
            }

            metricName = normalizedFlopsName;
          }

          // if there are at least two FLOPS metrics, aggregate their normalized
          // values
          if (summarizeFlops) {
            // INFO(PLUGIN_NAME ": FLOPS value set/add: %lu - %s(%d):%lf",
            // CDTIME_T_TO_TIME_T(time), metric->name, cpus[c], metricValue);

            if (-1.0 == flopsValues[c]) {
              flopsValues[c] = metricValue;
            } else {
              flopsValues[c] += metricValue;
            }

            // do not submit yet
            continue;
          }
        }

        _submit_value(_getMeasurementName(metric), metricName, cpus[c],
                      metricValue, time);
      }
    }
  }

  // submit metrics, if they have been summarized
  if (summarizeFlops) {
    for (int c = 0; c < numCPUs; c++) {
      _submit_value("likwid_cpu", normalizedFlopsName, cpus[c], flopsValues[c],
                    time);

      // reset counter value
      flopsValues[c] = -1.0;
    }
  }

  return 0;
}

static int likwid_plugin_init(void) {
  // INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  // set the cdtime based on the measurement time per group
  mcdTime = TIME_T_TO_CDTIME_T(mTime);

  int ret = _init_likwid();

  _setupGroups();

  return ret;
}

#ifndef TEST_LIWKID
/*! brief Resets the likwid group counters

Example notification on command line:
echo "PUTNOTIF severity=okay time=$(date +%s) plugin=likwid message=rstCtrs" |
socat - UNIX-CLIENT:$HOME/sw/collectd/collectd-unixsock echo "PUTNOTIF
severity=okay time=$(date +%s) plugin=likwid message=rstCtrs" | nc -U
/tmp/pika_collectd.sock
 */
static int likwid_plugin_notify(const notification_t *type, user_data_t *usr) {
  if (0 == strncmp(type->plugin, "likwid", 6)) {
    if (0 == strncmp(type->message, "rstCtrs", 7)) {
      _resetCounters();
    } else if (0 == strncmp(type->message, "disable", 7)) {
      INFO(PLUGIN_NAME ": Disable reading of Likwid metrics");
      plugin_disabled = true;
    } else if (0 == strncmp(type->message, "enable", 6)) {
      INFO(PLUGIN_NAME ": Enable reading of Likwid metrics");
      plugin_disabled = false;
    }
  }

  return 0;
}
#endif

static int likwid_plugin_finalize(void) {
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  // perfmon_finalize(); // segfault
  affinity_finalize();
  numa_finalize();
  topology_finalize();

  // free memory where CPU IDs are stored
  // INFO(PLUGIN_NAME ": free allocated memory");
  if (NULL != cpus) {
    free(cpus);
  }

  if (NULL != metricGroups) {
    for (int i = 0; i < numGroups; i++) {
      // memory for group names have been allocated with strdup
      if (NULL != metricGroups[i].name) {
        free(metricGroups[i].name);
      }
    }
    free(metricGroups);

    if (flopsValues) {
      free(flopsValues);
    }
  }

  return 0;
}

static const char *config_keys[] = {
    "NormalizeFlops",   "AccessMode", "Mtime",  "Groups",
    "PerSocketMetrics", "MaxValues",  "Verbose"};
static int config_keys_num = STATIC_ARRAY_SIZE(config_keys);

static int likwid_plugin_config(const char *key, const char *value) {
  // INFO(PLUGIN_NAME ": config: %s := %s", key, value);

  // use comma to separate metrics and metric groups
  // collectd converts commas in 'value' to spaces
  static char separator = ',';

  if (strcasecmp(key, "NormalizeFlops") == 0) {
    normalizeFlops = true;
    normalizedFlopsName = mystrdup(value);
  } else if (strcasecmp(key, "AccessMode") == 0) {
    accessMode = atoi(value);
  } else if (strcasecmp(key, "Mtime") == 0) {
    mTime = atoi(value);
    INFO(PLUGIN_NAME ": measure each metric group for %d sec\n", mTime);
  } else if (strcasecmp(key, "Verbose") == 0) {
    likwid_verbose = atoi(value);
  } else if (strcasecmp(key, "Groups") == 0) {
    // using separate config lines would not allows us to allocate the metric,
    // group array, because the number of metrics was unknown

    // count number of groups
    numGroups = 1;
    int i = 0;
    while (value[i] != '\0') {
      if (value[i] == separator) {
        numGroups++;
      }
      i++;
    }

    // allocate metric group array
    metricGroups = (metric_group_t *)malloc(numGroups * sizeof(metric_group_t));
    if (NULL == metricGroups) {
      ERROR(PLUGIN_NAME ": Could not allocate memory for metric groups: %s",
            value);
      return 1; // config failed
    }

    // inialize metric groups
    for (int i = 0; i < numGroups; i++) {
      metricGroups[i].id = -1;
      metricGroups[i].name = NULL;
      metricGroups[i].numMetrics = 0;
      metricGroups[i].metrics = NULL;
    }

    i = 0;
    char *grp_ptr;
    char *myvalue =
        mystrdup(value); // need a copy as strtok modifies the first argument
    grp_ptr = strtok(myvalue, &separator);
    while (grp_ptr != NULL) {
      // save group name
      metricGroups[i].name = mystrdup(grp_ptr);
      INFO(PLUGIN_NAME ": Found group: %s", grp_ptr);

      // get next group
      grp_ptr = strtok(NULL, &separator);

      i++;
    }
    // free(myvalue);
  } else if (strcasecmp(key, "PerSocketMetrics") == 0) {
    // count number of per socket metrics
    numSocketMetrics = 1;
    int i = 0;
    while (value[i] != '\0') {
      if (value[i] == separator) {
        numSocketMetrics++;
      }
      i++;
    }

    // allocate metric group array
    perSocketMetrics = (char **)malloc(numSocketMetrics * sizeof(char *));
    if (NULL == perSocketMetrics) {
      ERROR(PLUGIN_NAME
            ": Could not allocate memory for per socket metrics: %s",
            value);
      numSocketMetrics = 0;
      return 1; // config failed
    }

    // tokenize the string by separator
    i = 0;
    char *myvalue =
        mystrdup(value); // need a copy as strtok modifies the first argument
    char *metric_ptr = strtok(myvalue, &separator);
    while (metric_ptr != NULL) {
      // save metric name
      perSocketMetrics[i] = mystrdup(metric_ptr);
      INFO(PLUGIN_NAME ": Found per socket metric: %s", metric_ptr);

      // get next group
      metric_ptr = strtok(NULL, &separator);

      i++;
    }
    // free(myvalue);
  } else if (strcasecmp(key, "MaxValues") == 0) {
    // count number of thresholds
    if(strlen(value) == 0){
      ERROR(PLUGIN_NAME ": Empty string for MaxValues is not allowed!");
      return 1;
    }
    
    numMaxValues = 1;
    int i = 0;
    while (value[i] != '\0') {
      if (value[i] == separator) {
        numMaxValues++;
      }
      i++;
    }

    // allocate max values array
    maxValues = (max_value_t *)malloc(numMaxValues * sizeof(max_value_t));
    if (NULL == maxValues) {
      ERROR(PLUGIN_NAME ": Could not allocate memory for max values: %s",
            value);
      return 1; // config failed
    }
    
    i = 0;
    char *max_ptr;
    char *myvalue =
        mystrdup(value); // need a copy as strtok modifies the first argument
    max_ptr = strtok(myvalue, &separator);
    while (max_ptr != NULL) {
      char *sep2 = strchr(max_ptr, ':');
      
      if(sep2 == NULL) {
        ERROR(PLUGIN_NAME ": MaxValues requires a ':' as separator between metric and value!"); 
        return 1;
      }
      
      maxValues[i].maxValue = strtod(sep2 + 1, NULL);

      // save metric name
      *sep2 = '\0';
      maxValues[i].metricName = mystrdup(max_ptr);
      INFO(PLUGIN_NAME ": Skip %s values > %.2lf", max_ptr,
           maxValues[i].maxValue);

      // get next max value
      max_ptr = strtok(NULL, &separator);

      i++;
    }
    // free(myvalue);
  } else {
    return -1;
  }

  return 0;
}

#ifndef TEST_LIWKID

/*
 * This function is called after loading the plugin to register it with
 * collectd.
 */
void module_register(void) {
  plugin_register_config(PLUGIN_NAME, likwid_plugin_config, config_keys,
                         config_keys_num);
  plugin_register_read(PLUGIN_NAME, likwid_plugin_read);
  plugin_register_init(PLUGIN_NAME, likwid_plugin_init);
  plugin_register_shutdown(PLUGIN_NAME, likwid_plugin_finalize);
  plugin_register_notification(PLUGIN_NAME, likwid_plugin_notify,
                               /* user data = */ NULL);
  return;
}

#else

int main(int argc, char *argv[]) {
  // assume first argument to be the event group
  if (argc > 1) {
    for (int i = 1; i < argc; i++) {
      if (strncmp(argv[i], "-v", 2) == 0) {
        likwid_verbose = atoi(argv[i] + 2);
        fprintf(stderr, "Set LIKWID verbose level to %d\n", likwid_verbose);
      } else if (strncmp(argv[i], "-g", 2) == 0) {
        fprintf(stderr, "Use group(s) %s\n", argv[i] + 2);
        likwid_plugin_config("Groups", argv[i] + 2);
      } else if (strncmp(argv[i], "-m", 2) == 0) {
        fprintf(stderr, "Measurement time %s\n", argv[i] + 2);
        likwid_plugin_config("Mtime", argv[i] + 2);
      }
    }
  }

  if (numGroups == 0) {
    likwid_plugin_config("Groups", "BRANCH");
  }

  likwid_plugin_config("PerSocketMetrics", "mem_bw,rapl_power");

  // initialize LIKWID
  _init_likwid();

  CpuTopology_t cputopo = get_cpuTopology();

  fprintf(stderr,
          "Number of activeHWThreads: %d, numHWThreads: %d, numCoresPerSocket: "
          "%d, numThreadsPerCore: %d\n",
          cputopo->activeHWThreads, cputopo->numHWThreads,
          cputopo->numCoresPerSocket, cputopo->numThreadsPerCore);

  _setupGroups();

  // for(int i = 0; i < 100; i++) {
  while (true) {
    likwid_plugin_read();
  }

  // finalize LIKWID
  likwid_plugin_finalize();

  return 0;
}

#endif
