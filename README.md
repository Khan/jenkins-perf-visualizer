# jenkins-perf-visualizer
Visualize how your Jenkins builds are spending their time

## Preface

jenkins-perf-visualizer was developed for use by Khan Academy and is
being released publicly in the hopes it will be useful to others.  It
is not being actively maintained, and Khan Academy will resolve any
filed issues on a best-effort basis only.  (Pull requests are more
likely to be considered.)

Khan Academy is a non-profit organization with a mission to provide a
free, world-class education to anyone, anywhere.  You can help us in
that mission by [donating](https://khanacademy.org/donate) or looking
at [career opportunities](https://khanacademy.org/careers).

## Overview

This project provides various tools for analyzing and visualizing what
your Jenkins builds are doing over time.

Jenkins has some
[built-in visualization support](https://plugins.jenkins.io/pipeline-stage-view/),
 and there are also
[visualization plug-ins](https://wiki.jenkins.io/display/JENKINS/Yet+Another+Build+Visualizer+Plugin)
that work inside Jenkins.

This tools differs from those in the following respects:

1. It provides datapoints not only on a per-build basis, like most
plug-ins, or on a per-stage basis, like the built-in visualizer, but
also per-parallel-step.  This makes it easy to see, when running
various tasks in parallel, which is the critical path.

2. It shows time-taken visually, as a stacked bar graph.  Other tools
show time-takes as a number, making it hard to see at a glance where
the time is being spent.

3. It can graph a single "task" -- a collection of different build
jobs that are part of a workflow -- on a single graph.  Other tools
can only visualize a single build at a time, or, at best, historical
builds for a single job.

4. It is a standalone tool, not part of Jenkins.  Among other
advantages, this makes it easy to examine historical data after
Jenkins has deleted the information about a build.

5. The raw data used to generate the graph is stored in an
easy-to-parse format, making it easy to run analyses over the data,
use the data for alerts, etc.  For instance, in the example graph
below, you could easily compute for each of the `deploy-to-x` nodes
how often it was the one that took the longest to run.

6. It distinguishes between time a build spends running, vs waiting
for user input, sleeping, or waiting to start (because no executor is
available).  This gives valuable insight how to solve performance
problems.

Here is an example graph:

![visualization graph](https://github.com/Khan/jenkins-perf-visualizer/blob/main/example-graph.png?raw=true)

## Getting Started

TODO

### Configuring jenkins-perf-visualizer

TODO

## Implementation Details

jenkins-perf-visualizer uses the Jenkins API to get some information
about jobs and builds, but mostly depends on the Jenkins "Pipeline
Steps" page.
