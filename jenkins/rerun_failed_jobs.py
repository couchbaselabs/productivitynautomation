import json
from couchbase.cluster import Cluster, ClusterOptions
from couchbase.auth import PasswordAuthenticator
import sys
from optparse import OptionParser
from jenkins import Jenkins
from jenkinshelper import connect_to_jenkins
import logging
import traceback
import requests
import jenkins
import time
import os
import csv
from copy import deepcopy

logger = logging.getLogger("rerun_failed_jobs")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
ch.setFormatter(formatter)
logger.addHandler(ch)

WAITING_PATH = "waiting_for_main.csv"
COMPONENT_PROGRESS_PATH = "component_progress.csv"
RERUNS_PATH = "reruns.csv"

# Differences to the existing rerun script

# Runs are completely dependent on the couchbase bucket containing the runs rather than being hard coded. This means that additional components can be added without modifying this script
# Only a single pass is made whereas the existing script runs forever.
# Filtering is available for components, subcomponents, os
# Strategies such as common failures and failures with a higher failed test count can be used when a comparison build is specified.
# A single jenkins job is used rather than building 100+ test_suite_dispatcher jobs
# Dispatcher and non dispatcher jobs can be rerun (jobs without support for reruns are run with failed and successful tests)
# Dispatcher jobs are run with the same parameters as the fresh run e.g. extra testrunner parameters, gerrit cherrypick
# Won't dispatch if similar job already running with option to stop similar job before dispatching


def parse_arguments():
    parser = OptionParser()

    parser.add_option("-u", "--url", dest="jenkins_url", default='http://qa.sc.couchbase.com', help="Jenkins URL")
    parser.add_option("-n", "--noop", dest="noop", help="Just print hanging jobs, don't stop them", action="store_true", default=False)
    parser.add_option("-a", "--aborted", dest="aborted", help="Include aborted jobs even with no failed tests", action="store_true", default=False)
    parser.add_option("-s", "--stop", dest="stop", help="Stop a duplicate running job before starting the rerun", action="store_true", default=False)
    parser.add_option("-f", "--failed", dest="failed", help="Include jobs with failed tests", action="store_true", default=False)
    parser.add_option("-p", "--previous-builds", dest="previous_builds", help="Previous builds to compare for regressions or common failures")
    parser.add_option("-b", "--build", dest="build", help="Build version to rerun e.g. 7.0.0-3594")
    parser.add_option("--server", dest="server", help="Couchbase server host", default="172.23.121.84")
    parser.add_option("--username", dest="username", help="Couchbase server username", default="Administrator")
    parser.add_option("--password", dest="password", help="Couchbase server password", default="password")
    parser.add_option("--dispatcher-jobs", dest="dispatcher_jobs", help="only rerun jobs managed by a dispatcher", action="store_true", default=False)
    parser.add_option("--os", dest="os", help="List of operating systems: e.g. win, magma, centos, ubuntu, mac, debian, suse, oel")
    parser.add_option("--exclude-os", dest="exclude_os", help="List of operating systems to exclude: e.g. win, magma, centos, ubuntu, mac, debian, suse, oel")
    parser.add_option("--components", dest="components", help="List of components to include")
    parser.add_option("--subcomponents", dest="subcomponents", help="List of subcomponents to include")
    parser.add_option("--exclude-components", dest="exclude_components", help="List of components to exclude e.g. magma")
    parser.add_option("--override-executor", dest="override_executor", help="Force passing of -j option to test dispatcher", action="store_true", default=False)
    parser.add_option("--s3-logs-url", dest="s3_logs_url", help="Amazon S3 bucket url that stores historical jenkins logs", default="http://cb-logs-qe.s3-website-us-west-2.amazonaws.com")
    parser.add_option("--strategy", dest="strategy", help="Which strategy should be used to find jobs to rerun", choices=("common", "regression"))
    parser.add_option("--pools-threshold-percent", dest="pools_threshold_percent", help="Percent of machines that must be available in each pool before reruns should begin", type="int", default=50)
    parser.add_option("--pools-threshold-num", dest="pools_threshold_num", help="If pool has this many machines available, ignore pools-threshold-percent, reruns can begin", type="int", default=20)
    parser.add_option("--jobs-threshold", dest="jobs_threshold", help="Percent of jobs that must be complete before reruns should begin", type="int", default=90)
    parser.add_option("--include-pools", dest="include_pools", help="Pools to include in pools-threshold e.g. 12hrreg,magma,regression,os_certification")
    parser.add_option("--exclude-pools", dest="exclude_pools", help="Pools to exclude in pools-threshold e.g. elastic-xdcr")
    parser.add_option("--wait", dest="wait_for_main_run", help="Wait for main run to finish (using pool and job thresholds) before starting reruns", action="store_true", default=False)
    parser.add_option("--timeout", dest="timeout", help="Stop reruns after timeout hours even if all main run jobs haven't completed", type="int", default=24)
    parser.add_option("--sleep", dest="sleep", help="Time to sleep between checking for reruns (minutes)", type="int", default=5)
    parser.add_option("--max-reruns", dest="max_reruns", help="Max number of times to rerun a job (only applicable when this script is run more than once)", type="int", default=1)
    parser.add_option("--max-failed-reruns", dest="max_failed_reruns", help="Max number of times to rerun a job if the rerun was worse than the fresh run", type="int", default=1)
    parser.add_option("--output", dest="output")
    parser.add_option("--dispatch-delay", dest="dispatch_delay", help="Time to wait between dispatch calls (seconds)", type="int", default=10)
    parser.add_option("--merge-pools", dest="merge_pools", help="List of pools that can be used interchangeably")
    parser.add_option("--maintain-threshold", dest="maintain_threshold", help="Check pool availability every time before dispatching", action="store_true", default=False)
    parser.add_option("--override-dispatcher", dest="override_dispatcher", default="test_suite_dispatcher_multiple_pools")
    parser.add_option("--failed-jobs", dest="failed_jobs", default=False, action="store_true")
    parser.add_option("--rerun-build", dest="rerun_build", help="Couchbase server build to rerun with, same as --build if not supplied")

    options, _ = parser.parse_args()

    if options.previous_builds:
        options.previous_builds = options.previous_builds.split(",")

    if options.os:
        options.os = options.os.split(",")

    if options.exclude_os:
        options.exclude_os = options.exclude_os.split(",")

    if options.components:
        options.components = options.components.split(",")

    if options.exclude_components:
        options.exclude_components = options.exclude_components.split(",")

    if options.include_pools:
        options.include_pools = options.include_pools.split(",")

    if options.exclude_pools:
        options.exclude_pools = options.exclude_pools.split(",")

    if options.merge_pools:
        options.merge_pools = options.merge_pools.split(",")

    if options.subcomponents:
        options.subcomponents = options.subcomponents.split(",")

    options.sleep = options.sleep * 60

    return options

def parameters_from_actions(actions):
    parameters = {}
    for a in actions:
        try:
            if a["_class"] == "hudson.model.ParametersAction":
                for param in a["parameters"]:
                    if "name" in param and "value" in param:
                        parameters[param['name']] = param['value']
        except KeyError:
            pass
    return parameters


def parameters_for_job(server: Jenkins, name, number, version_number=None, s3_logs_url=None):
    try:
        info = server.get_build_info(name, number)
    except jenkins.JenkinsException:
        if version_number and s3_logs_url:
            try:
                info = requests.get("{}/{}/jenkins_logs/{}/{}/jobinfo.json".format(
                    s3_logs_url, version_number, name, number)).json()
            except:
                raise Exception("couldn't get parameters from s3")
        else:
            raise ValueError(
                "no version number for build missing from jenkins")
    return parameters_from_actions(info["actions"])


def get_running_builds(server: Jenkins):
    # running_builds = []
    # get all running builds so we know if a duplicate job is already running
    running_builds = server.get_running_builds()
    queued_builds = server.get_queue_info()

    builds_with_params = []

    for build in queued_builds:
        if "task" in build and "name" in build["task"]:
            try:
                parameters = parameters_from_actions(build["actions"])
                builds_with_params.append({
                    "name": build["task"]["name"],
                    "parameters": parameters
                })
            except Exception:
                traceback.print_exc()

    for build in running_builds:
        try:
            parameters = parameters_for_job(server, build['name'], build['number'])
            builds_with_params.append({
                "name": build['name'],
                "number": build['number'],
                "parameters": parameters
            })
        except Exception:
            traceback.print_exc()

    return builds_with_params

def job_name_from_url(jenkins_server, url):
    return url.replace("{}/job/".format(jenkins_server), "").strip("/")

def get_duplicate_jobs(running_builds, job_name, parameters, options):
    duplicates = []

    for running_build in running_builds:
        try:

            if "dispatcher_params" in parameters:

                # check if duplicate executor job
                if running_build["name"] == job_name:

                    # executor job with different os
                    if running_build["parameters"]["os"] != parameters["os"]:
                        continue

                    # executor job with different component
                    if running_build["parameters"]["component"] != parameters["component"]:
                        continue

                    # executor job with different subcomponent
                    if running_build["parameters"]["subcomponent"] != parameters["subcomponent"]:
                        continue

                    # executor job with different version_number
                    if running_build["parameters"]["version_number"] != parameters["version_number"]:
                        continue

                # check if duplicate dispatcher job
                else:
                    dispatcher_name = job_name_from_url(options.jenkins_url, parameters["dispatcher_params"]['dispatcher_url'])

                    if dispatcher_name == "test_suite_dispatcher":
                        dispatcher_names = [dispatcher_name, "test_suite_dispatcher_multiple_pools"]
                    else:
                        dispatcher_names = [dispatcher_name]

                    duplicate = False

                    # Hack until main dispatcher supports multiple pools
                    for dispatcher_name in dispatcher_names:

                        if running_build["name"] != dispatcher_name:
                            continue

                        if running_build["parameters"]["OS"] != parameters["os"]:
                            continue

                        if running_build["parameters"]["component"] != parameters["component"]:
                            continue

                        if running_build["parameters"]["version_number"] != parameters["version_number"]:
                            continue

                        # if dispatcher subcomponent is not None or "" then list of subcomponents must not contain parameters["subcomponent"]
                        if running_build["parameters"]["subcomponent"] not in ["None", ""] and parameters["subcomponent"] not in running_build["parameters"]["subcomponent"].split(","):
                            continue

                        duplicate = True
                        break

                    if not duplicate:
                        continue

            else:
                # standalone job with different name
                if running_build["name"] != job_name:
                    continue

                # has version_number field and is not the same
                if "version_number" in running_build["parameters"] and running_build["parameters"]["version_number"] != parameters["version_number"]:
                    continue

                # otherwise job name equal and can't differentiate by version_number so duplicate

            duplicates.append(running_build)

        except Exception:
            traceback.print_exc()
            continue

    return duplicates


def latest_jenkins_builds(options):
    latest_builds = []
    response = requests.get(options.jenkins_url + "/api/json?tree=jobs[url,name,builds[url,number,actions[parameters[name,value]]]]").json()
    for job in response["jobs"]:
        for build in job["builds"]:
            parameters = parameters_from_actions(build["actions"])
            latest_builds.append({
                "name": job['name'],
                "number": build['number'],
                "parameters": parameters
            })
    return latest_builds


# jinja (jenkins collector) can take a few minutes to collect a build
# make sure there is no build in jenkins newer than in the bucket
def newer_build_in_jenkins(job_name, job, parameters, latest_jenkins_builds, options):
    duplicates = get_duplicate_jobs(latest_jenkins_builds, job_name, parameters, options)
    for duplicate in duplicates:
        # get_duplicate_jobs can return dispatcher jobs
        if duplicate["name"] == job_name and duplicate["number"] > job["build_id"]:
            return True, duplicates
    return False, duplicates


def get_jobs_still_to_run(options, cluster: Cluster, server: Jenkins):
    jobs = list(cluster.query("SELECT name, component, url, build_id, `build` FROM server WHERE `build`= '{}' AND url LIKE '{}/job/%'".format(options.previous_builds[0], options.jenkins_url)))
    previous_jobs = set()

    # filter out components not in options.components
    if options.components or options.exclude_components or options.os or options.exclude_os:
        for job in jobs:
            try:
                job_name = job_name_from_url(options.jenkins_url, job['url'])

                parameters = parameters_for_job(server,
                    job_name, job['build_id'], job['build'], options.s3_logs_url)

                if passes_component_filter(job, parameters, options) and passes_os_filter(job, parameters, options):
                    previous_jobs.add(job["name"])

            except Exception:
                pass
    else:
        previous_jobs = set([job["name"] for job in jobs])

    current_jobs = set(cluster.query("SELECT raw name FROM server WHERE `build`= '{}' AND url LIKE '{}/job/%'".format(options.build, options.jenkins_url)))
    still_to_run = previous_jobs.difference(current_jobs)

    components_in_last_run = list(cluster.query("SELECT component, count(*) count from server where `build` = '{}' AND url LIKE '{}/job/%' group by component".format(options.previous_builds[0], options.jenkins_url)))
    components_in_current_run = list(cluster.query("SELECT component, count(*) count from server where `build` = '{}' AND url LIKE '{}/job/%' group by component".format(options.build, options.jenkins_url)))

    component_map = {}

    for comp in components_in_last_run:
        component_map[comp['component']] = { "prev": int(comp['count']), "curr": 0 }
    
    for comp in components_in_current_run:
        if comp['component'] in component_map:
            component_map[comp['component']]['curr'] = int(comp['count'])
        else:
            component_map[comp['component']] = { "prev": 0, "curr": int(comp['count']) }
        

    return previous_jobs, still_to_run, component_map


def wait_for_main_run(options, cluster: Cluster, server: Jenkins):
    ready_for_reruns = False

    while not ready_for_reruns:

        ready_for_reruns = True

        try:
            previous_jobs, still_to_run, component_map = get_jobs_still_to_run(options, cluster, server)

            if len(previous_jobs) > 0:
                percent_jobs_complete = ((len(previous_jobs) - len(still_to_run)) / len(previous_jobs)) * 100
            else:
                percent_jobs_complete = 100

            if percent_jobs_complete < options.jobs_threshold:
                ready_for_reruns = False

            log_progress(options, previous_jobs, still_to_run, component_map)

            if ready_for_reruns:
                break
            else:
                logger.info("Waiting for main run to near completion: {} out of {} jobs ({:.2f}%) complete".format(len(previous_jobs) - len(still_to_run), len(previous_jobs), percent_jobs_complete))

        except Exception:
            traceback.print_exc()
            ready_for_reruns = False

        time.sleep(options.sleep)


def filter_query(query: str, options):
    # options.failed -> failures or no tests passed
    # options.aborted -> result is aborted

    filter = ""

    if options.failed:
        filter = "failCount > 0 OR failCount = totalCount"
    
    if options.aborted:
        if filter != "":
            filter += " OR "
        filter += "result = 'ABORTED'"

    if options.failed_jobs:
        if filter != "":
            filter += " OR "
        filter += "result = 'FAILURE'"

    if filter != "":
        query += " AND ({})".format(filter)

    return query

def all_failed_jobs(cluster: Cluster, options):
    query = "SELECT `build`, build_id, component, failCount, name, os, result, totalCount, url FROM server WHERE `build` = '{}' AND url LIKE '{}/job/%'".format(options.build, options.jenkins_url)
    query = filter_query(query, options)

    logger.info("running query {}".format(query))

    jobs = list(cluster.query(query))

    return jobs

def passes_component_filter(job, parameters, options):
    if options.components:
        if ("component" in parameters and parameters["component"] not in options.components) and job['component'].lower() not in options.components:
            return False
    
    if options.subcomponents and ("subcomponent" not in parameters or parameters['subcomponent'] not in options.subcomponents):
        return False
    
    if options.exclude_components:
        if ("component" in parameters and parameters["component"] in options.exclude_components) or job["component"].lower() in options.exclude_components:
            return False

    return True

def passes_os_filter(job, parameters, options):
    if options.os:
        if ("os" in parameters and parameters["os"] not in options.os) and job["os"].lower() not in options.os:
            return False
    
    if options.exclude_os:
        if ("os" in parameters and parameters["os"] in options.exclude_os) or job["os"].lower() in options.exclude_os:
            return False

    return True

def passes_max_rerun_filter(cluster: Cluster, job, options):
    query = "select raw os.`{}`.`{}`.`{}` from greenboard where `build` = '{}' and type = 'server'".format(job["os"], job["component"], job["name"], options.build)

    all_runs = len(list(cluster.query(query))[0])
    reruns = all_runs - 1

    return reruns < options.max_reruns

def passes_pool_threshold(cluster: Cluster, dispatcher_name, dispatcher_params, options, pool_thresholds_hit):
    pool_ids = dispatcher_params["serverPoolId"].split(",")
    pools = pool_ids.copy()

    query = "select count(*) as count from `QE-server-pool` where state = '{0}' and (poolId = '{1}' or '{1}' in poolId)"

    for pool in pools:

        available = list(cluster.query(query.format("available", pool)))[0]['count']
        booked = list(cluster.query(query.format("booked", pool)))[0]['count']
        total = available + booked

        if total == 0:
            continue

        percent_available = (available/total) * 100

        if percent_available >= options.pools_threshold_percent or available >= options.pools_threshold_num:
            if pool not in pool_thresholds_hit:
                pool_thresholds_hit.append(pool)
        else:
            if options.maintain_threshold or pool not in pool_thresholds_hit:
                return False

    # if any pool ids in serverPoolId are in options.merge_pools 
    # then add the other pools in options.merge_pools to serverPoolId
    # e.g. serverPoolId = os_certification,regression options.merge_pools=regression,12hrreg
    # serverPoolId becomes os_certification,regression,12hrreg
    # only test_suite_dispatcher supports multiple pools for now
    if dispatcher_name == "test_suite_dispatcher" and options.merge_pools:
        for pool in pool_ids:
            if pool in options.merge_pools:
                for pool in options.merge_pools:
                    if pool not in pools:
                        pools.append(pool)
    
    dispatcher_params["serverPoolId"] = ",".join(pools)

    return True

def rerun_worse_helper(all_runs, options):
    # we will only try a worse rerun again if there was a rerun and the number of worse reruns is less than `max_failed_reruns`

    if len(all_runs) < 2:
        return False

    fresh_run = all_runs[len(all_runs) - 1]
    latest_rerun = all_runs[0]

    def worse(run, fresh_run):
        # if fresh run was failure, failCount will be 0 so anything higher would cause another rerun
        if fresh_run["result"] == "FAILURE" and run["result"] != "FAILURE":
            return False
        return run["failCount"] > fresh_run["failCount"] or run["totalCount"] < fresh_run["totalCount"]

    worse_reruns = list(filter(lambda run: worse(run, fresh_run), all_runs[:-1]))

    return worse(latest_rerun, fresh_run) and len(worse_reruns) <= options.max_failed_reruns

def rerun_worse(cluster: Cluster, job, options):
    query = "select raw os.`{}`.`{}`.`{}` from greenboard where `build` = '{}' and type = 'server'".format(job["os"], job["component"], job["name"], options.build)
    all_runs = list(cluster.query(query))[0]
    return rerun_worse_helper(all_runs, options)


def filter_jobs(jobs, cluster: Cluster, server: Jenkins, options, queue, already_rerun):
    logger.info("filtering {} jobs".format(len(jobs)))
    running_builds = get_running_builds(server)
    latest_builds = latest_jenkins_builds(options)
    for job in jobs:

        run_url = job["url"] + str(job["build_id"])

        if job["name"] in queue or run_url in already_rerun:
            continue

        try:

            reasons = []

            if job["result"] == "ABORTED":
                reasons.append("aborted")
            
            if job["result"] == "FAILURE":
                reasons.append("failure")

            if job["failCount"] == job["totalCount"]:
                reasons.append("no tests passed")

            if job["failCount"] > 0:
                reasons.append("failed tests")

            rerun_was_worse = rerun_worse(cluster, job, options)

            # if rerun was worse we skip these checks
            if not rerun_was_worse:

                if not passes_max_rerun_filter(cluster, job, options):
                    logger.debug("skipping {} (already rerun max times)".format(job["name"]))
                    continue

            job_name = job_name_from_url(options.jenkins_url, job['url'])

            parameters = parameters_for_job(server,
                job_name, job['build_id'], job['build'], options.s3_logs_url)

            if "dispatcher_params" in parameters:
                dispatcher_params = json.loads(parameters['dispatcher_params'][11:])
                parameters["dispatcher_params"] = dispatcher_params
                # TODO: Remove when CBQE-6336 fixed
                if "component" not in dispatcher_params:
                    logger.debug("skipping {} (invalid dispatcher_params)".format(job["name"]))
                    continue

            # only run dispatcher jobs
            if "dispatcher_params" not in parameters and options.dispatcher_jobs:
                logger.debug("skipping {} (non dispatcher job)".format(job["name"]))
                continue

            is_newer, newer_builds = newer_build_in_jenkins(job_name, job, parameters, latest_builds, options)
            already_running = get_duplicate_jobs(running_builds, job_name, parameters, options)

            if is_newer:
                should_skip = False
                for build in newer_builds:
                    if build not in already_running:
                        should_skip = True
                        break
                if should_skip:
                    logger.debug("skipping {} (newer build in jenkins)".format(job["name"]))
                    continue

            if len(already_running) > 0:
                if options.stop:
                    should_skip = False
                    for build in already_running:
                        if "number" in build:
                            logger.info(
                                "aborting {}/{}".format(build['name'], build['number']))
                            if not options.noop:
                                server.stop_build(build['name'], build['number'])
                        else:
                            should_skip = True
                    if should_skip:    
                        # duplicate queued job, don't stop it
                        logger.debug("skipping {} (already queued)".format(job["name"]))
                        continue
                else:
                    logger.debug("skipping {} (already running or waiting to be dispatched)".format(job["name"]))
                    continue

            if not passes_component_filter(job, parameters, options):
                logger.debug("skipping {} (component not included)".format(job["name"]))
                continue

            if not passes_os_filter(job, parameters, options):
                logger.debug("skipping {} (os not included)".format(job["name"]))
                continue

            if options.strategy:

                if options.strategy == "common":
                    query = "select raw count(*) from server where name = '{}' and `build` in {}".format(job['name'], options.previous_builds)
                    query = filter_query(query, options)
                    common_count = list(cluster.query(query))[0]
                    
                    # job wasn't common across all previous builds
                    if common_count != len(options.previous_builds):
                        logger.debug("skipping {} (not common across all previous builds)".format(job["name"]))
                        continue
                    else:
                        reasons.append("common")

                elif options.strategy == "regression":
                    # regression (if name in previous build and failCount or totalCount was different)

                    query = "select raw os.`{}`.`{}`.`{}` from greenboard where `build` = '{}' and type = 'server'".format(job["os"], job["component"], job["name"], options.previous_builds[0])
                    previous_job_runs = list(cluster.query(query))
                    if len(previous_job_runs) == 1:
                        previous_job_runs = previous_job_runs[0]
                    else:
                        previous_job_runs = []
                    previous_job = next(filter(lambda run: run["olderBuild"] == False, previous_job_runs), None)
                    # if no previous job then this is either a new job or 
                    # that job wasn't run last time so don't filter

                    if previous_job is not None:
                        prev_fail_count = int(previous_job['failCount'])
                        prev_total_count = int(previous_job['totalCount'])
                        prev_result = previous_job["result"]
                        curr_fail_count = int(job['failCount'])
                        curr_total_count = int(job['totalCount'])
                        curr_result = job["result"]

                        if prev_fail_count == curr_fail_count and prev_total_count == curr_total_count and prev_result == curr_result:
                            logger.debug("skipping {} (not regression)".format(job["name"]))
                            continue
                        else:
                            reasons.append("regression")
                            job["prev_total_count"] = prev_total_count
                            job["prev_fail_count"] = prev_fail_count
                            job["prev_pass_count"] = prev_total_count - prev_fail_count
                            job["prev_build_id"] = int(previous_job["build_id"])
                            job["prev_result"] = prev_result

            if len(reasons) == 0:
                reasons.append("forced")

            job["parameters"] = parameters
            job["reasons_for_rerun"] = reasons
            queue[job["name"]] = job
            already_rerun.add(run_url)

        except Exception:
            traceback.print_exc()
            continue


def rerun_jobs(queue, server: Jenkins, cluster, pool_thresholds_hit, options):
    already_dispatching = {}

    triggered = []

    for job in queue.values():
        job_name = job_name_from_url(options.jenkins_url, job['url'])

        try:

            parameters = deepcopy(job["parameters"])

            if 'dispatcher_params' not in parameters:

                if options.rerun_build and "version_number" in parameters and parameters["version_number"] == options.build:
                    parameters["version_number"] = options.rerun_build

                if not options.noop:
                    server.build_job(job_name, parameters)

                logger.info("Triggered {} with parameters {}".format(job_name, parameters))

                triggered.append(job)

            else:

                dispatcher_params = parameters['dispatcher_params']

                # This is not needed because the executor is defined at the test level in QE-Test-Suites using the framwork key
                # e.g. -jython, -TAF

                if options.override_executor and job_name != "test_suite_executor":
                    executor_suffix = job_name.replace(
                        "test_suite_executor-", "")
                    dispatcher_params['executor_suffix'] = executor_suffix

                # can only be a rerun if the job ran to completion
                if job['result'] != "ABORTED":
                    # this is a rerun
                    dispatcher_params['fresh_run'] = False

                if options.rerun_build and "version_number" in dispatcher_params and dispatcher_params["version_number"] == options.build:
                    dispatcher_params["version_number"] = options.rerun_build

                dispatcher_name = job_name_from_url(options.jenkins_url, dispatcher_params['dispatcher_url'])

                # invalid parameter
                dispatcher_params.pop("dispatcher_url")

                # we determine component and subcomponent by the params of the job not dispatcher job
                # e.g. only 1 subcomponent might need to be rerun
                dispatcher_params["component"] = "None"
                dispatcher_params["subcomponent"] = "None"

                if dispatcher_name not in already_dispatching:
                    already_dispatching[dispatcher_name] = {}
                already_dispatching_job = already_dispatching[dispatcher_name]

                if parameters['component'] not in already_dispatching_job:
                    already_dispatching_job[parameters['component']] = []
                already_dispatching_component = already_dispatching_job[parameters['component']]

                found = False
                for subcomponents in already_dispatching_component:
                    if subcomponents['params'] == dispatcher_params:
                        found = True
                        if parameters['subcomponent'] not in subcomponents['subcomponents']:
                            subcomponents['subcomponents'].append(
                                parameters['subcomponent'])
                            subcomponents["jobs"].append(job)

                if not found:
                    already_dispatching_component.append({
                        "params": dispatcher_params,
                        "subcomponents": [parameters['subcomponent']],
                        "jobs": [job]
                    })
        
        except Exception:
            traceback.print_exc()
            continue

    for [dispatcher_name, components] in already_dispatching.items():
        for [component_name, component] in components.items():
            for job in component:
                try:
                    dispatcher_params = job['params']
                    dispatcher_params['component'] = component_name
                    dispatcher_params['subcomponent'] = ",".join(
                        job['subcomponents'])

                    if not passes_pool_threshold(cluster, dispatcher_name, dispatcher_params, options, pool_thresholds_hit):
                        continue

                    queued_builds = server.get_queue_info()
                    queued_build_names = set()

                    for build in queued_builds:
                        if "task" in build and "name" in build["task"]:
                            queued_build_names.add(build["task"]["name"])

                    actual_dispatcher_name = dispatcher_name

                    if dispatcher_name == "test_suite_dispatcher" and options.override_dispatcher:
                        actual_dispatcher_name = options.override_dispatcher

                    # skip if build for this dispatcher in queue
                    if actual_dispatcher_name in queued_build_names:
                        time.sleep(options.dispatch_delay)
                        continue

                    final_params = []

                    for [key, value] in dispatcher_params.items():
                        if key == "serverPoolId":
                            pools = value.split(",")
                            for pool in pools:
                                final_params.append(("serverPoolId", pool))
                        else:
                            final_params.append((key, value))

                    if not options.noop:
                        server.build_job(actual_dispatcher_name, final_params)
                        time.sleep(options.dispatch_delay)

                    logger.info("Triggered {} with parameters {}".format(actual_dispatcher_name, dispatcher_params))

                    # each subcomponent will be its own job
                    for j in job["jobs"]:
                        triggered.append(j)

                except:
                    traceback.print_exc()
                    continue

    for job in triggered:
        queue.pop(job["name"])

    return triggered

def log_paths(options):
    major_version = options.build.split("-")[0]
    if options.output:
        waiting_path = os.path.join(options.output, major_version + WAITING_PATH)
        component_progress_path = os.path.join(options.output, major_version + COMPONENT_PROGRESS_PATH)
        reruns_path = os.path.join(options.output, major_version + RERUNS_PATH)
    else:
        waiting_path = major_version + WAITING_PATH
        component_progress_path = major_version + COMPONENT_PROGRESS_PATH
        reruns_path = major_version + RERUNS_PATH
    return waiting_path, component_progress_path, reruns_path


def log_progress(options, previous_jobs, still_to_run, component_map):
    waiting_path, component_progress_path, _ = log_paths(options)

    # TODO: * 1000 for grafana milliseconds
    now = time.time()

    with open(waiting_path, 'a') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow([now, len(previous_jobs) - len(still_to_run), len(previous_jobs), (options.jobs_threshold / 100) * len(previous_jobs)])

    with open(component_progress_path, 'a') as csvfile:
        csv_writer = csv.writer(csvfile)
        for [component, counts] in component_map.items():
            csv_writer.writerow([now, counts['curr'], counts['prev'], component])

def setup_logs(options):
    waiting_path, component_progress_path, reruns_path = log_paths(options)

    with open(waiting_path, 'w') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(["timestamp", "completed_jobs", "total_jobs", "rerun_threshold"])

    with open(component_progress_path, 'w') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(["timestamp", "completed_jobs", "total_jobs", "component", "rerun_threshold"])

    with open(reruns_path, 'w') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(["timestamp", "name", "url", "pass_count", "fail_count", "total_count", "result", "reasons_for_rerun", "prev_url", "prev_pass_count", "prev_fail_count", "prev_total_count", "prev_result", "previous_builds", "current_build"])
   
def log_reruns(options, jobs):
    _, _, reruns_path = log_paths(options)

    now = time.time()

    with open(reruns_path, 'a') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_rows = []
        previous_builds = options.previous_builds or []
        previous_builds = " ".join(previous_builds)
        for job in jobs:
            reasons_for_rerun = "|".join(job["reasons_for_rerun"])
            prev_total_count = job["prev_total_count"] if "prev_total_count" in job else ""
            prev_fail_count = job["prev_fail_count"] if "prev_fail_count" in job else ""
            prev_pass_count = job["prev_total_count"] - job["prev_fail_count"] if "prev_total_count" in job and "prev_fail_count" in job else ""
            prev_url = job["url"] + str(job["prev_build_id"]) if "prev_build_id" in job else ""
            prev_result = job.get("prev_result") or ""
            csv_rows.append([now, job['name'], job["url"]+str(job["build_id"]), job["totalCount"] - job["failCount"], job["failCount"], job["totalCount"], job["result"], reasons_for_rerun, prev_url, prev_pass_count, prev_fail_count, prev_total_count, prev_result, previous_builds, options.build])
        csv_writer.writerows(csv_rows)

def validate_options(options, cluster: Cluster):
    if not options.build:
        logger.error("No build given")
        sys.exit(1)

    if (options.strategy or options.wait_for_main_run) and (not options.previous_builds or len(options.previous_builds) == 0):
        logger.info("--previous-builds not specified, trying the calculate...")
        version = options.build.split("-")[0]
        previous_builds = list(cluster.query("select raw `build` from greenboard where `build` like '{}%' and type = 'server' and totalCount > 18500 and `build` != '{}' group by `build` order by `build` desc limit 1".format(version, options.build)))
        if len(previous_builds) == 0 or previous_builds[0] == options.build:
            logger.warning("couldn't determine previous build automatically, ignoring --wait and --strategy parameters")
            options.strategy = None
            options.wait_for_main_run = False
        else:
            logger.info("previous build set to {}".format(previous_builds[0]))
            options.previous_builds = [previous_builds[0]]

    if options.strategy and options.strategy == "regression" and len(options.previous_builds) != 1:
        logger.error("regression strategy must specify 1 previous build for comparison")
        sys.exit(1)

    if options.components and options.exclude_components:
        logger.error("both include and exclude components specified")
        sys.exit(1)

    if options.subcomponents and len(options.components) > 1:
        logger.error("Can't supply multiple components with subcomponents")
        sys.exit(1)


if __name__ == "__main__":
    options = parse_arguments()
    cluster = Cluster('couchbase://{}'.format(options.server), ClusterOptions(PasswordAuthenticator(options.username, options.password)))
    validate_options(options, cluster)
    logger.debug(options)
    setup_logs(options)
    server = connect_to_jenkins(options.jenkins_url)

    if options.wait_for_main_run:
        wait_for_main_run(options, cluster, server)

    pool_thresholds_hit = []
    queue = {}
    already_rerun = set()

    # timeout after 20 hours
    timeout = time.time() + (options.timeout * 60 * 60)

    while True:
        try:
            jobs = all_failed_jobs(cluster, options)
            filter_jobs(jobs, cluster, server, options, queue, already_rerun)

            if len(jobs) > 0:
                triggered_jobs = rerun_jobs(queue, server, cluster, pool_thresholds_hit, options)
                log_reruns(options, triggered_jobs)

            num_still_to_run = 0

            if options.wait_for_main_run:
                previous_jobs, still_to_run, component_map = get_jobs_still_to_run(options, cluster, server)
                num_still_to_run = len(still_to_run)
                if len(still_to_run) > 0:
                    logger.info("{} more jobs from the main run to finish".format(len(still_to_run)))
                    for job_name in still_to_run:
                        logger.debug(job_name)

                log_progress(options, previous_jobs, still_to_run, component_map)
            
            if len(queue) == 0 and (time.time() > timeout or (options.wait_for_main_run and num_still_to_run == 0)):
                break

            if len(queue) > 0:
                logger.info("{} jobs in rerun queue".format(len(queue)))
                for job_name in queue:
                    logger.debug(job_name)

        except Exception:
            traceback.print_exc()

        time.sleep(options.sleep)