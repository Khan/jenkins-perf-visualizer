#!/usr/bin/python

"""Emit a chart that shows where time is spent during specified jenkins builds.

The output is a chart.  The x axis is seconds since the beginning
of the input jenkins build until the end.  (If multiple builds are specified
it's the beginning of the first build until the end of the last one.)

The y axis is a set of "nodes".  A node captures the time taken by
a block of Jenkins pipeline steps:
   1) Every stage() step begins a new node, labeled with the name of
      the stage, and includes all the commands in that stage.
   2) Every branch of a parallel() step begins a new node, labeled
      with the name of that branch, and includes all the commands
      run by that branch.
   3) "<jenkins build name>" holds the overall jenkins build.
(Note that our helper jenkins functions, like notify() define
some stages of their own, so every Jenkins build has a "main"
node which contains everything inside a "notify" block.)

Each node can be in one of several states:
1) Running
2) Sleeping, via a sleep(), waitFor(), or prompt() jenkins step.
   Note that sleeping via shell (`sh("sleep 10")`) is *not*
   counted here -- we have no way of knowing what sh() is doing!
3) Waiting for an executor.  This happens when a node decides to
   start running on a jenkins worker, and it has to wait for a
   new gce machine to get started up.

Our output for each node is a horizontal bar, saying what the
node is doing during the given second.  Each node has its own
color, with varying brightnesses to say what the node is doing
(so if the coordinate (50, 6) is light yellow, it means that node
#6 is waiting for an executor during second 50, whereas if it's
dark yellow it's running during second 50).

This script works in three main stages:
1) It figures out what steps were run, and how long they took,
   by scraping the html from a jenkins /flowGraphTable
   ("Pipeline steps") page, and collects them into a runtime tree.
   (The `Step` class.)
2) It figures out which Node each part of that tree is running
   over (by looking for stage() and parallel() steps), and
   linearizes the steps in a Node into a set of time-ranges
   where that node is doing something.  It then looks at each
   of the steps in that node to categorize each moment of time
   into a category: running, sleeping, or waiting.
   (The `Node` class.)
3) It constructs and emits a graph based on the node data.
   (`create_html()`.)
"""
import argparse
import logging
import os
import webbrowser

from jenkins_perf_visualizer import builds
from jenkins_perf_visualizer import configuration
from jenkins_perf_visualizer import fetch
from jenkins_perf_visualizer import html
from jenkins_perf_visualizer import jenkins
from jenkins_perf_visualizer import nodes
from jenkins_perf_visualizer import steps


def main(config, buildses, title, html_file):
    """jenkins_* vars are not needed if all builds are .data files."""
    build_datas = []
    for build in buildses:
        if build.endswith('.data'):  # Used a cached file to avoid the network
            (job, build_id) = os.path.basename(build[:-len('.data')]).replace(
                '--', '/').split(':')
            (step_html, build_params, build_start_time) = (
                fetch.fetch_from_datafile(build))
            outfile = build
        else:
            jenkins_client = jenkins.get_client(config)
            datadir = config.get('datadir', '/tmp')
            (job, build_id) = build.split(':')
            (step_html, build_params, build_start_time, outfile) = (
                fetch.fetch_build(job, build_id, datadir, jenkins_client))

        step_root = steps.parse_pipeline_steps(step_html)
        node_root = nodes.steps_to_nodes(step_root)
        build_datas.append(builds.BuildData(
            config, job, build_id, build_start_time, build_params, node_root))

    build_datas.sort(key=lambda jd: jd.build_start_time_ms)

    if not html_file:  # they didn't specify on the commandline
        html_file = outfile.replace('.data', '.html')
    output_html = html.create_html(config, build_datas, title)
    with open(html_file, 'wb') as f:
        f.write(output_html.encode('utf-8'))

    if config.get('openWebpageInBrowser', True):
        webbrowser.open(html_file)


if __name__ == '__main__':
    logging.basicConfig(format="[%(asctime)s %(levelname)s] %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        'build', nargs='+',
        help=("Jenkins builds to fetch, e.g. deploy/build-webapp:1543 "
              "OR a data-filename like deploy-build-webapp:1543.data."))
    parser.add_argument(
        '-o', '--output-filename',
        help=("The name to use for the output .html file.  Defaults to "
              "a name based on the first input build."))
    parser.add_argument(
        '-t', '--title',
        help=("The title to use for the html graph, overriding the rule "
              "for automatically generating the title in config.json."))

    # Lets you specify a config file to control everything else.
    configuration.add_config_arg(parser)
    # Lets you override the values in the config file on a per-run basis.
    configuration.add_datadir_arg(parser)
    configuration.add_no_open_webpage_in_browser_arg(parser)

    parser.add_argument('-v', '--verbose', action='store_true',
                        help=('Log more data when running.'))

    args = parser.parse_args()
    config = configuration.load(args)

    logging.getLogger().setLevel(
        logging.DEBUG if args.verbose else logging.INFO)

    main(config, args.build, args.title, args.output_filename)
