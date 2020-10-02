"""Routines to create html files based on BuildData objects."""
import json
import os
import time

import builds


def create_html(job_datas):
    """Return an html page that will render our flame-like chart.

    We use custom CSS to do this.
    """
    deploy_start_time_ms = min(j.data['jobStartTimeMs'] for j in job_datas)
    deploy_end_time_ms = max(j.data['jobEndTimeMs'] for j in job_datas)
    title = ('%s (%s)'
             % (' + '.join(sorted(set(j.data['title'] for j in job_datas))),
                time.strftime("%Y/%m/%d %H:%M:%S",
                              time.localtime(deploy_start_time_ms / 1000))))
    deploy_data = {
        'jobs': [j.data for j in job_datas],
        'title': title,
        'colors': builds.COLORS,
        'deployStartTimeMs': deploy_start_time_ms,
        'deployEndTimeMs': deploy_end_time_ms,
    }

    visualizer_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(visualizer_dir, 'visualize.html')) as f:
        template = f.read()
    with open(os.path.join(visualizer_dir, 'visualize.js')) as f:
        js = f.read()
    with open(os.path.join(visualizer_dir, 'visualize.css')) as f:
        css = f.read()

    return template \
        .replace('{{js}}', js) \
        .replace('{{css}}', css) \
        .replace('{{data}}', json.dumps(deploy_data, sort_keys=True, indent=2))
