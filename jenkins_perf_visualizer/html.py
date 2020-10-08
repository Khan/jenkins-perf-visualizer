"""Routines to create html files based on BuildData objects."""
import json
import os
import time

from jenkins_perf_visualizer import colors


def create_html(config, build_datas, title=None):
    """Return an html page that will render our flame-like chart.

    We use custom CSS to do this.
    """
    # A "task" is a collection of related jenkins builds.  The idea
    # is that a single task, such as deploying your app, may involve
    # several jenkins builds run in concert.
    task_start_time_ms = min(j.data['buildStartTimeMs'] for j in build_datas)
    task_end_time_ms = max(j.data['buildEndTimeMs'] for j in build_datas)
    if not title:
        title = ' / '.join(sorted(set(j.data['title'] for j in build_datas)))
    subtitle = time.strftime("%Y/%m/%d %H:%M:%S",
                             time.localtime(task_start_time_ms / 1000))
    task_data = {
        'builds': [j.data for j in build_datas],
        'title': title,
        'subtitle': subtitle,
        'colorToId': colors.color_to_id(config),
        'taskStartTimeMs': task_start_time_ms,
        'taskEndTimeMs': task_end_time_ms,
    }

    visualizer_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(visualizer_dir, 'visualize.html')) as f:
        template = f.read()
    with open(os.path.join(visualizer_dir, 'visualize.js')) as f:
        js = f.read()
    with open(os.path.join(visualizer_dir, 'visualize.css')) as f:
        css = f.read()

    return (template
                .replace('{{js}}', js)
                .replace('{{css}}', css)
                .replace('{{data}}', json.dumps(task_data,
                                                sort_keys=True, indent=2)))
