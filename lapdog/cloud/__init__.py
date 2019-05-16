# This module defines utilities for the cloud function api

from .utils import __API_VERSION__
import tempfile
import shutil
import subprocess
import glob
import os
import json

__FUNCTION_MAPPING__ = {
    'create_submission': 'submit.py',
    'abort_submission': 'abort.py',
    'existence': 'internal.py',
    'redacted': 'internal.py',
    'check_abort': 'internal.py',
    'quotas': 'quotas.py',
    'register': 'register.py',
    'query_account': 'query.py',
    'insert_resolution': 'resolution.py'
}

RESOLUTION_URL = "https://us-central1-a-graubert.cloudfunctions.net/resolve-" + __API_VERSION__['resolve']

def _deploy(function, endpoint, service_account=None, project=None, overload_version=None):
    if overload_version is None:
        overload_version = __API_VERSION__[endpoint]
    with tempfile.TemporaryDirectory() as tempdir:
        with open(os.path.join(tempdir, 'requirements.txt'), 'w') as w:
            w.write('google-auth==1.6.3\n')
            w.write('google-cloud-storage==1.14.0\n')
            w.write('google-cloud-kms==1.0.0\n')
            w.write('cryptography==2.3\n')
        shutil.copyfile(
            os.path.join(
                os.path.dirname(__file__),
                __FUNCTION_MAPPING__[function]
            ),
            os.path.join(tempdir, 'main.py')
        )
        shutil.copyfile(
            os.path.join(
                os.path.dirname(__file__),
                'utils.py'
            ),
            os.path.join(tempdir, 'utils.py')
        )
        cmd = 'gcloud {project} functions deploy {endpoint}-{version} --entry-point {function} --runtime python37 --trigger-http --source {path} {service_account}'.format(
            endpoint=endpoint,
            version=overload_version,
            function=function,
            path=tempdir,
            service_account='' if service_account is None else ('--service-account '+service_account),
            project='' if project is None else ('--project '+project)
        )
        print(cmd)
        subprocess.check_call(
            cmd,
            shell=True,
            executable='/bin/bash'
        )

def __generate_alert_internal(title, alert_type, content, text=None):
    """
    You're welcome to use this function, but you won't have permissions
    """
    assert alert_type in {'critical', 'warning', 'info'}
    blob = utils.getblob(
        'gs://lapdog-alerts/{}'.format(title)
    )
    alert = {
        'type': alert_type,
        'content': content
    }
    if text is not None:
        alert['text'] = text
    blob.upload_from_string(json.dumps(alert))
