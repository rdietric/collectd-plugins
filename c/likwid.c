#define _POSIX_C_SOURCE	199309L //required for timespec and nanosleep() in c99
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <float.h>
#include <time.h>

#include <likwid.h>

#ifdef TEST_LIWKID
#include <inttypes.h>
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

#define CDTIME_T_TO_TIME_T(t)                                                  \
  (time_t) { (time_t)(((t) + (1 << 29)) >> 30) }

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
#ifdef DEBUG
#define DEBUG(...) plugin_log(0, __VA_ARGS__)
#else
#define DEBUG(...)
#endif
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

static bool plugin_disabled = false;

/*! counter register access mode (default: direct access / perf_event) */
static int accessMode = 0;

/*! measurement time per event/metric group in nanoseconds (default: 10 sec) */
struct timespec mTime = {10, 0};

/*! measurement time per group in cdtime_t */
static cdtime_t mTimeCd = 0;

/*! Likwid verbosity output level (default: 1) */
static int likwid_verbose = 1;

/*! Normalize FLOPS to single precision? (default: false) */
static bool normalizeFlops = false;

/*! Summarize multiple FLOPS metrics into single precision FLOPS (can be true
 * only if multiple FLOPS metrics are monitored) */
static bool summarizeFlops = false;

/*! Name of the normalized FLOPS metric */
static char *normalizedFlopsName = "flops_any";

/*! storage to normalize FLOPS values */
static double *flopsValues = NULL;

/*! \brief Maximum values for metrics */
typedef struct {
  char *metricName;
  double maxValue;
} max_value_t;
static max_value_t *maxValues = NULL;
static int numMaxValues = 0;
static uint64_t counterLimit = 0;

/*! \brief Metric type */
typedef struct {
  char *name;     /*!< metric name */
  uint8_t xFlops; /*!< if > 0, it is a FLOPS metric and the value is
                       the multiplier for normalization */
  bool perCpu; /*!< true, if values are per CPU, otherwise per socket is assumed
                */
  double *perCoreValues; /*! Sum up HW thread values to core granularity */
  double maxValue;
} metric_t;

/*! \brief Metric group type */
typedef struct {
  int id;            /*!< group ID */
  char *name;        /*!< group name */
  int numMetrics;    /*!< number of metrics in this group */
  metric_t *metrics; /*!< metrics in this group */
} metric_group_t;

static int numGroups = 0;
static metric_group_t *metricGroups = NULL;

/* required thread array */
static int numThreads = 0;    /**< number of HW threads to be monitored */
static int *hwThreads = NULL; /**< array of apic IDs to be monitored */

/*! per-socket metrics */
static int numSockets = 0;
static int *socketThreadIndices =
    NULL; /*!< threads containing the per-socket data */
static int numSocketMetrics = 0;
static char **perSocketMetrics = NULL; /*!< array of per socket metric names */

/*!< Optional: sum up hardware thread values to cores, if SMT is enabled. */
static bool summarizePerCore = false;
static uint32_t numCores = 0;

/*! \brief Thread to core mapping structures */
static int *coreIndices = NULL;  /*!< Index into the per core data array */
static uint32_t *coreIds = NULL; /*!< ID of the physical core by core index */

/*! Define an own strdup() as in C99 no strdup prototype is available. */
static char *mystrdup(const char *s) {
  size_t len = strlen(s) + 1;
  char *result = (char *)malloc(len);
  if (result == (char *)0)
    return (char *)0;
  return (char *)memcpy(result, s, len);
}

/*! Determines by metric name, whether this is a per CPU or per socket
metric. The default is "per CPU" */
static bool _isMetricPerCPU(const char *metric) {
  for (int i = 0; i < numSocketMetrics; i++) {
    if (0 == strncmp(perSocketMetrics[i], metric, 6)) {
      return false;
    }
  }

  return true;
}

/*! \brief Initializes the event sets to be monitored. */
static void _setupGroups() {
  if (NULL == metricGroups) {
    ERROR(PLUGIN_NAME "No metric groups allocated! Plugin not initialized?");
    return;
  }

  INFO(PLUGIN_NAME ": Setup metric group(s)");

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
          metrics[m].perCpu = _isMetricPerCPU(metrics[m].name);

          // normalize flops, if enabled
          if (normalizeFlops && 0 == strncmp("flops", metrics[m].name, 5)) {
            numFlopMetrics++;

            size_t flopsStrLen = strlen(metrics[m].name);

            // if metric is named exactly like the user-defined normalized FLOPS name, normalization of FLOPS is not needed
            if (0 == strcmp(normalizedFlopsName, metrics[m].name)) {
              normalizeFlops = false;
              metrics[m].xFlops = 0;
              INFO(PLUGIN_NAME ": Found metric %s. No normalization needed.", metrics[m].name);
            }
            // double precision to single precision = factor 2
            else if (flopsStrLen >= 8 && 0 == strncmp("dp", metrics[m].name + 6, 2)) {
              metrics[m].xFlops = 2;
            }
            // // avx to single precision = factor 4
            else if (flopsStrLen >= 9 && 0 == strncmp("avx", metrics[m].name + 6, 3)) {
              metrics[m].xFlops = 4;
            } else // assume single precision otherwise
            {
              metrics[m].xFlops = 1;
            }
          } else {
            metrics[m].xFlops = 0;
          }

          // if HW thread values should be summarized to cores, allocate per
          // metric arrays
          if (summarizePerCore) {
            metrics[m].perCoreValues =
                (double *)malloc(numCores * sizeof(double));
            if (NULL == metrics[m].perCoreValues) {
              WARNING(PLUGIN_NAME
                      ": Malloc failed. Cannot summarize per core!");
              summarizePerCore = false;
            }

            // initialize to invalid values, which will not be submitted
            for (int i = 0; i < numCores; i++) {
              metrics[m].perCoreValues[i] = -1.0;
            }
          }

          // set maximum value of metric
          if(counterLimit != 0) {
            metrics[m].maxValue = (double)counterLimit;
          } else {
            metrics[m].maxValue = DBL_MAX;
          }
          
          for (int i = 0; i < numMaxValues; i++) {
            if (0 == strncmp(metrics[m].name, maxValues[i].metricName,
                             strlen(maxValues[i].metricName))) {
              metrics[m].maxValue = maxValues[i].maxValue;
            }
          }
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
    INFO(PLUGIN_NAME ": Different FLOPS are aggregated.");
    summarizeFlops = true;

    flopsValues = (double *)malloc(numThreads * sizeof(double));
    if (flopsValues) {
      // initialize with -1 (invalid value)
      for(int i = 0; i < numThreads; i++){
        flopsValues[i] = -1.0;
      }
    } else {
      WARNING(PLUGIN_NAME ": Could not allocate memory for normalization of "
                          "FLOPS. Disable summarization of FLOPS.");
      summarizeFlops = false;
    }
  }

  // no need to handle different FLOPS in the same metric group, as this could
  // be handled directly in the Likwid metric group files
}

static int likwid_plugin_finalize(void) {
  INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  // perfmon_finalize(); // segfault
  affinity_finalize();
  numa_finalize();
  topology_finalize();

  // free memory where CPU IDs are stored
  // INFO(PLUGIN_NAME ": free allocated memory");
  if (NULL != hwThreads) {
    free(hwThreads);
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

/*! \brief Initialize the LIKWID monitoring environment */
static int _init_likwid(void) {
  topology_init();
  numa_init();
  affinity_init();

  CpuTopology_t cputopo = get_cpuTopology();
  HWThread *threadPool = cputopo->threadPool;
  numThreads = cputopo->numHWThreads;

  hwThreads = (int *)malloc(numThreads * sizeof(int));
  if (NULL == hwThreads) {
    ERROR(PLUGIN_NAME ": malloc of APIC ID array failed!");
    likwid_plugin_finalize();
    return 1;
  }

  for (int i = 0; i < numThreads; i++) {
    hwThreads[i] = (int)threadPool[i].apicId;
  }
  HPMmode(accessMode);
  perfmon_setVerbosity(likwid_verbose);
  perfmon_init(numThreads, hwThreads);

  // determine the HW threads that provide the per-socket data
  numSockets = cputopo->numSockets;
  socketThreadIndices = malloc(numSockets * sizeof(int));
  if (NULL == socketThreadIndices) {
    ERROR(PLUGIN_NAME ": malloc of socket thread index array failed!");
    return 1;
  }

  int currentSocketIdx = 0;
  for (int i = 0; i < numThreads; i++) {
    uint32_t socketId = threadPool[i].packageId;
    bool found = false;
    for (int s = 0; s < currentSocketIdx; s++) {
      if (socketThreadIndices[s] == socketId) {
        found = true;
        break;
      }
    }

    if (!found) {
      socketThreadIndices[currentSocketIdx] = i;
      INFO(PLUGIN_NAME ": Collecting per-socket metrics with thread %d", i);

      currentSocketIdx++;
      if (currentSocketIdx == numSockets) {
        break;
      }
    }
  }

  // handle per-core summarization
  uint32_t numThreadsPerCore = cputopo->numThreadsPerCore;
  if (summarizePerCore == false || numThreadsPerCore == 1) {
    summarizePerCore = false;
  } else {
    INFO(PLUGIN_NAME ": collect per core (%u threads per core)", numThreadsPerCore);

    numCores = cputopo->numCoresPerSocket * numSockets;
    coreIndices = (int *)malloc(numThreads * sizeof(int));
    coreIds = (uint32_t *)malloc(numCores * sizeof(uint32_t));
    if (NULL == coreIndices || coreIds == NULL) {
      ERROR(PLUGIN_NAME ": memory allocation for CPU core data failed!");
      likwid_plugin_finalize();
      return 1;
    }

    // initialize core value array to invalid indices
    for (int i = 0; i < numThreads; i++) {
      coreIndices[i] = -1;
    }

    // preparation to get per-core values
    int currentCoreIdx = 0;
    for (int i = 0; i < numThreads; i++) {
      if (coreIndices[i] == -1) { // if core index has not been set
        coreIndices[i] = currentCoreIdx;

        // iterate over following thread indices
        for (int j = i + 1; j < numThreads; j++) {
          if (threadPool[i].coreId == threadPool[j].coreId) {
            coreIndices[j] = currentCoreIdx;
            coreIds[currentCoreIdx] = threadPool[i].coreId;
          }
        }

        currentCoreIdx++;
      }
      DEBUG(PLUGIN_NAME ": HWthread:CoreID:CoreArrayIdx %d:%" PRIu32 ":%d",
           hwThreads[i], threadPool[i].coreId, coreIndices[i]);
    }
  }

  CpuInfo_t cpuinfo = get_cpuInfo();
  uint32_t counterBitWidth = cpuinfo->perf_width_ctr;
  if(counterBitWidth > 0){
    counterLimit = ((uint64_t)1 << (counterBitWidth + 1)) - 1;
    INFO(PLUGIN_NAME ": metric max value (%"PRIu32" bits): %"PRIu64, counterBitWidth, counterLimit);
  } 

  return 0;
}

#ifndef TEST_LIWKID
/*! \brief Sets the counters that have been setup with _setupGroups().

This is only reasonable, if direct access mode is used and other tools can
change the configuration of the MSR registers.
*/
static void _setCounters(void) {
  INFO(PLUGIN_NAME ": Set counters configuration for %d groups!", numGroups);

  for (int g = 0; g < numGroups; g++) {
    if (metricGroups[g].id < 0) {
      return;
    }

    perfmon_setCountersConfig(metricGroups[g].id);
  }
}
#endif

static const char *_getMeasurementName(metric_t *metric) {
  if (metric->perCpu) {
    return "likwid_cpu";
  } else {
    return "likwid_socket";
  }
}

/*! brief: Determine whether the given index in the thread pool contains the
per socket data */
static bool _hasSocketData(int thread_array_idx) {
  for (int s = 0; s < numSockets; s++) {
    if (thread_array_idx == socketThreadIndices[s]) {
      return true;
    }
  }
  return false;
}

#ifdef TEST_LIWKID

static void _submit_value(const char *measurement, const char *metric, int cpu,
                         double value, cdtime_t time) {
  // drop invalid values
  if (value == -1.0) {
    return;
  }

  fprintf(stderr, "%d: %s - %s = %lf (%" PRIu64 ")\n", cpu, measurement, metric,
          value, time);
}

#else

/*! \brief Submit a metric value.

Collectd metrics are serialized as follows:
host "/" plugin ["-" plugin instance] "/" type ["-" type instance]
e.g. taurusi2001/likwid_socket-0/ipc

The type field is statically set to 'likwid'.

@param [in] measurement the measurement name, which maps to the plugin name
(can be either 'likwid_cpu' or 'likwid_socket')
@param [in] metric name of the metric to be submitted as type instance
@param [in] cpu the CPU core, which is mapped to plugin instance
@param [in] value metric value to be submitted as Collectd gauge type
@param [in] time timestamp when the metric was acquired
*/
static void _submit_value(const char *measurement, const char *metric, int cpu,
                          double value, cdtime_t time) {
  // drop invalid values
  if (value == -1.0) {
    return;
  }

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

  cdtime_t time = cdtime() + mTimeCd * numGroups;

  // read from likwid
  for (int g = 0; g < numGroups; g++) {
    int gid = metricGroups[g].id;
    if (gid < 0) {
      INFO(PLUGIN_NAME ": No eventset specified for group %s",
           metricGroups[g].name);
      if(-1 == nanosleep(&mTime, NULL)) {
        WARNING(PLUGIN_NAME ": nanosleep has been interrupted");
      }
      continue;
    }

    if (0 != perfmon_setupCounters(gid)) {
      INFO(PLUGIN_NAME ": Could not setup counters for group %s",
           metricGroups[g].name);
      continue;
    }

    // measure counters for setup group
    perfmon_startCounters();
    if(-1 == nanosleep(&mTime, NULL)) {
      WARNING(PLUGIN_NAME ": nanosleep has been interrupted");
    }
    perfmon_stopCounters();

    // int nmetrics = perfmon_getNumberOfMetrics(gid);
    int nmetrics = metricGroups[g].numMetrics;

    // INFO(PLUGIN_NAME ": Measured %d metrics for %d CPUs for group %s (%ld.%ld
    // sec)", nmetrics, numThreads, metricGroups[g].name, mTime.tv_sec, mTime.tv_nsec);

    // if we change thread and metric loop order, one physical core array is
    // enough (no array per metric necessary)

    // for all hardware threads
    for (int c = 0; c < numThreads; c++) {
      // for all metrics in the group
      for (int m = 0; m < nmetrics; m++) {
        // c is the index in the array used as argument to perfmon_init()
        double metricValue = perfmon_getLastMetric(gid, m, c);
        metric_t *metric = &(metricGroups[g].metrics[m]);

        //INFO(PLUGIN_NAME ": %lu - %s(%d):%lf", CDTIME_T_TO_TIME_T(time),
        //metric->name, hwThreads[c], metricValue);

        // skip cores that do not provide values for per socket metrics
        if (!metric->perCpu && !_hasSocketData(c)) {
          //INFO("Skip HW thread %d for socket metric %s", c, metric->name);
          continue;
        }

        if (isfinite(metricValue) == 0) {
          continue;
        }

        char *metricName = metric->name;

#ifdef DEBUG
        // REMOVE: check that we write the value for the correct metric
        if (0 != strcmp(metricName, perfmon_getMetricName(gid, m))) {
          WARNING(PLUGIN_NAME ": Something went wrong!!!");
        }
#endif

        if (metricValue > metric->maxValue) {
          INFO(PLUGIN_NAME ": Skipping outlier for %s (%d): %.1lf", metricName, c,
               metricValue);
          continue;
        }

        // special handling for FLOPS metrics
        if (metric->xFlops > 0) {
          // if user requested FLOPS normalization (to single precision)
          if (normalizeFlops) {
            // normalize FLOPS that are not already single precision (if
            // requested)
            if (metric->xFlops > 1 && metricValue > 0) {
              metricValue *= metric->xFlops;
            }

            metricName = normalizedFlopsName;
          }

          // if multiple FLOPS metrics, aggregate their normalized values
          if (summarizeFlops) {
            // INFO(PLUGIN_NAME ": FLOPS value set/add: %lu - %s(%d):%lf",
            // CDTIME_T_TO_TIME_T(time), metric->name, cpus[c], metricValue);

            int idx = c;
            if (summarizePerCore) {
              idx = coreIndices[c];
            }

            if (-1.0 == flopsValues[idx]) {
              flopsValues[idx] = metricValue;
            } else {
              flopsValues[idx] += metricValue;
            }

            // do not submit yet
            continue;
          }
        }

        if (summarizePerCore) {
          int idx = coreIndices[c];
          if (metric->perCoreValues[idx] == -1.0) {
            metric->perCoreValues[idx] = metricValue;
          } else {
            metric->perCoreValues[idx] += metricValue;
          }
        } else {
          _submit_value(_getMeasurementName(metric), metricName, hwThreads[c],
                        metricValue, time);
        }
      }
    }
  }

  if (summarizePerCore) {
    for (int g = 0; g < numGroups; g++) {
      for (int c = 0; c < numCores; c++) {
        for (int m = 0; m < metricGroups[g].numMetrics; m++) {
          metric_t *metric = &(metricGroups[g].metrics[m]);
          const char* metricName = metric->name;

          if (metric->xFlops > 0) {
            // ignore FLOP values, if summarization of FLOPS is enabled
            if (summarizeFlops) {
              continue;
            }

            if (normalizeFlops) {
              metricName = normalizedFlopsName;
            }
          }
          
          _submit_value(_getMeasurementName(metric), metricName, coreIds[c],
                        metric->perCoreValues[c], time);

          metric->perCoreValues[c] = -1.0;
        }
      }
    }
  }

  // submit the summarized FLOPS
  if (summarizeFlops) {
    int arrayLen = numThreads;
    int *cpuIds = hwThreads;
    if (summarizePerCore) {
      arrayLen = numCores;
      cpuIds = (int *)coreIds;
    }

    for (int i = 0; i < arrayLen; i++) {
      _submit_value("likwid_cpu", normalizedFlopsName, cpuIds[i],
                    flopsValues[i], time);

      // reset counter value
      flopsValues[i] = -1.0;
    }
  }

  return 0;
}

static int likwid_plugin_init(void) {
  // INFO(PLUGIN_NAME ": %s:%d", __FUNCTION__, __LINE__);

  // set the cdtime based on the measurement time per group
  mTimeCd = TIMESPEC_TO_CDTIME_T(&mTime);

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
  if (type->plugin == NULL || (0 == strncmp(type->plugin, "likwid", 6))) {
    if (0 == strncmp(type->message, "rstCtrs", 7)) {
      _setCounters();
    } else if (0 == strncmp(type->message, "disable", 7)) {
      INFO(PLUGIN_NAME ": Disable reading of metrics.");
      plugin_disabled = true;
    } else if (0 == strncmp(type->message, "enable", 6)) {
      INFO(PLUGIN_NAME ": Enable reading of metrics.");
      plugin_disabled = false;
    }
  }

  return 0;
}
#endif

static const char *config_keys[] = {
    "NormalizeFlops",   "AccessMode", "Mtime",   "Groups",
    "PerSocketMetrics", "MaxValues",  "PerCore", "Verbose"};
static int config_keys_num = STATIC_ARRAY_SIZE(config_keys);

static int likwid_plugin_config(const char *key, const char *value) {
  // INFO(PLUGIN_NAME ": config: %s := %s", key, value);

  // use comma to separate metrics and metric groups
  // collectd converts commas in 'value' to spaces
  static char separator = ',';

  if (strcasecmp(key, "NormalizeFlops") == 0) {
    normalizeFlops = true;
    normalizedFlopsName = mystrdup(value);
    INFO(PLUGIN_NAME ": nomalize FLOPS to single precision (%s)", normalizedFlopsName);
  } else if (strcasecmp(key, "AccessMode") == 0) {
    accessMode = atoi(value);
  } else if (strcasecmp(key, "Mtime") == 0) {
    double mtd = strtod(value, NULL);
    mTime.tv_sec = (time_t)mtd;
    //mtd += 0.5e-9; // sleep a little less is fine
    mTime.tv_nsec = (mtd - mTime.tv_sec) * 1000000000L;
    INFO(PLUGIN_NAME ": measure each metric group for %.3lf sec\n", mtd);
  } else if (strcasecmp(key, "PerCore") == 0) {
    summarizePerCore = true;
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
    if (strlen(value) == 0) {
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

      if (sep2 == NULL) {
        ERROR(PLUGIN_NAME ": MaxValues requires a ':' as separator between "
                          "metric and value!");
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
      } else if (strncmp(argv[i], "-percore", 8) == 0) {
        fprintf(stderr, "Summarize per core\n");
        likwid_plugin_config("PerCore", "");
      } else if (strncmp(argv[i], "-normalizeflops", 14) == 0) {
        fprintf(stderr, "Normalize FLOPS\n");
        likwid_plugin_config("NormalizeFlops", "flops_any");
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
