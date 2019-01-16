# Submission Gateway
# Use as a modular component for adapter to interact with submission
# If submission JSON does not contain version field or version is 1, gcloud compute instances ssh
# If submission JSON contains version and version is 2, ssh -i {lapdog ssh token} {instance ip}
# If SSH fails for any reason, save the exception and try reading the log
# If the log is not found, then you're SOL

from functools import lru_cache
import requests
import subprocess
from hashlib import md5
from .cache import cached, cache_fetch, cache_write
import time
import warnings
import crayons
import os
import json
from .cloud import get_token_info, ld_project_for_namespace, ld_meta_bucket_for_project, getblob, proxy_group_for_user
from urllib.parse import quote
import sys

id_rsa = os.path.join(
    os.path.expanduser('~'),
    '.ssh',
    'id_rsa'
)

credentials_file = os.path.join(
    os.path.expanduser('~'),
    '.config',
    'gcloud',
    'application_default_credentials.json'
)

@cached(60, 1)
def get_account():
    return subprocess.run(
        'gcloud config get-value account',
        shell=True,
        stdout=subprocess.PIPE
    ).stdout.decode().strip()

@cached(10)
def get_access_token(account=None):
    if account is None:
        account = get_account()
    token = cache_fetch('token', 'access-token', md5(account.encode()).hexdigest(), google='credentials')
    if token and get_token_expired(token):
        token = None
    if token is None:
        if not os.path.isfile(credentials_file):
            raise FileNotFoundError("Application Default Credentials not found. Please run `gcloud auth application-default login`")
        with open(credentials_file) as r:
            credentials = json.load(r)
        response = requests.post(
            'https://www.googleapis.com/oauth2/v4/token',
            data={
                'client_id': credentials['client_id'],
                'client_secret': credentials['client_secret'],
                'refresh_token': credentials['refresh_token'],
                'grant_type': 'refresh_token'
            }
        )
        if response.status_code == 200:
            data = response.json()
            token = data['access_token']
            expiry = int(time.time() + int(data['expires_in']))
            cache_write(token, 'token', 'access-token', md5(account.encode()).hexdigest(), google='credentials')
            cache_write(str(expiry), 'token', 'expiry', token=md5(token.encode()).hexdigest())
        else:
            raise ValueError("Unable to refresh access token (%d) : %s" % (response.status_code, response.text))
    return token

def get_token_expired(token):
    expiry = cache_fetch('token', 'expiry', token=md5(token.encode()).hexdigest())
    if expiry is None:
        try:
            data = get_token_info(token)
            if data is None:
                return True
            expiry = int(time.time() + int(data['expires_in']))
            cache_write(str(expiry), 'token', 'expiry', token=md5(token.encode()).hexdigest())
        except:
            return True
    return int(expiry) < time.time()

def generate_core_key(ld_project):
    warnings.warn("Generating new root authentication key for project")
    response = requests.post(
        "https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{account}/keys".format(
            project=quote(ld_project, safe=''),
            account=quote('lapdog-worker@{}.iam.gserviceaccount.com'.format(ld_project), safe='')
        ),
        headers={
            'Authorization': 'Bearer ' + get_access_token()
        }
    )
    if response.status_code == 200:
        getblob(
            'gs://{bucket}/worker_key.json'.format(
                bucket=ld_meta_bucket_for_project(ld_project)
            )
        ).upload_from_string(response.text.encode())
    else:
        print("(%d) : %s" % (response.status_code, response.text), file=sys.stderr)
        raise ValueError("Could not generate core key")


class Gateway(object):
    """Acts as an interface between local lapdog and any resources behind the project's API"""

    def __init__(self, namespace):
        self.namespace = namespace
        self.project = ld_project_for_namespace(namespace)

    @classmethod
    def initialize_lapdog_for_project(cls, billing_id, project_id):
        """
        Initializes the lapdog execution API on the given firecloud project.
        Charges for operating the Lapdog API and for executing jobs will be billed
        to the provided billing id.
        The current gcloud account (when this function is executed) will be the Owner
        of the service and the only user capable of using it.
        Call authorize_user to allow another user to use the service
        """
        cmd = (
            'gcloud projects create {project_id}'.format(
                project_id=ld_project_for_namespace(project_id)
            )
        )
        print("Creating project")
        print(cmd)
        # subprocess.check_call(cmd, shell=True)
        cmd = (
            'gcloud beta billing projects link {project_id} --billing-account '
            '{billing_id}'.format(
                project_id=ld_project_for_namespace(project_id),
                billing_id=billing_id
            )
        )
        print("Enabling billing")
        print(cmd)
        # subprocess.check_call(cmd, shell=True)
        # TODO
        print("Creating Signing Key")
        cmd = (
            'gcloud --project {project} kms keyrings create lapdog --location us'.format(
                project=ld_project_for_namespace(project_id)
            )
        )
        print(cmd)
        # subprocess.check_call(cmd, shell=True)
        cmd = (
            'gcloud --project {project} alpha kms keys create lapdog-sign --location us --keyring'
            ' lapdog --purpose asymmetric-signing --default-algorithm '
            'RSA_SIGN_PSS_3072_SHA256 --protection-level SOFTWARE'.format(
                project=ld_project_for_namespace(project_id)
            )
        )
        print(cmd)
        # subprocess.check_call(cmd, shell=True)

        print("TODO : SETUP LAPDOG ROLES")
        print("Creating Core Service Account")
        cmd = (
            'gcloud --project {project} iam service-accounts create lapdog-worker --display-name lapdog-worker'.format(
                project=ld_project_for_namespace(project_id)
            )
        )
        print(cmd)
        # subprocess.check_call(cmd, shell=True)
        print("Creating Cloud Functions Service Account")
        cmd = (
            'gcloud --project {project} iam service-accounts create lapdog-functions --display-name lapdog-functions'.format(
                project=ld_project_for_namespace(project_id)
            )
        )
        print(cmd)
        # subprocess.check_call(cmd, shell=True)
        functions_account = 'lapdog-functions@{}.iam.gserviceaccount.com'.format(ld_project_for_namespace(project_id))
        print("Creating Metadata bucket while service accounts are created")
        cmd = (
            'gsutil mb -c Standard -l us-central1 -p {project} {bucket}'.format(
                project=ld_project_for_namespace(project_id),
                bucket=ld_meta_bucket_for_project(ld_project_for_namespace(project_id))
            )
        )
        print(cmd)
        # subprocess.check_call(cmd, shell=True)
        print("Waiting for service account creation...")
        time.sleep(30)
        print("Issuing Core Service Account Key")
        with tempfile.TemporaryDirectory() as temp:
            tempname = os.path.join(temp, 'key.json')
            cmd = (
                "gcloud --project {project} iam service-accounts keys create "
                "--iam-account lapdog-worker@{project}.iam.gserviceaccount.com {dest}".format(
                    project=ld_project_for_namespace(project_id),
                    dest=tempname
                )
            )
            print(cmd)
            # subprocess.check_call(cmd, shell=True)
            print("Copying Service Account Key to Metadata Bucket")
            blob = getblob(
                'gs://{bucket}/auth_key.json'.format(
                    bucket=ld_meta_bucket_for_project(ld_project_for_namespace(project_id))
                )
            )
            blob.upload_from_filename(tempname)
            print("Updating Key Metadata ACL")
            acl = blob.acl
            for entity in acl.get_entities():
                if entity.type == 'project':
                    if entity.identifier.startswith('editors-'):
                        entity.revoke_owner()
                    elif entity.identifier.startswith('viewers-'):
                        entity.revoke_read()
            acl.user(functions_account).grant_read()
            acl.save()

        print("Deploying Cloud Functions")
        from .cloud import _deploy
        # _deploy('create_submission', 'submit', functions_account)
        # _deploy('abort_submission', 'abort', functions_account)
        Gateway.grant_access_to_user(
            project_id,
            get_account(),
            True
        )

    @classmethod
    def grant_access_to_user(cls, project_id, target_account, is_moderator=False):
        """
        Grants the listed account access to the lapdog execution API for the given
        firecloud project. NOTE: This does not grant the user access to any data
        in any workspaces. This allocates a new service account for the user and
        grants required permissions to interact with the API.

        If is_moderator is set to True, the user will also have permissions to grant
        other users access to the lapdog execution API
        """
        print("TODO : GRANT ROLES TO TARGET")
        print("TODO : ISSUE SERVICE ACCOUNT FOR TARGET")
        print("TODO : Create user-execution-group in firecloud if not already exists")
        print("TODO : Add new service account to user-execution-group")

    def create_submission(self, workspace, bucket, submission_id, workflow_options=None, memory=3):
        """
        Sends a request through the lapdog execution API to start a new submission.
        Takes the local submission ID.
        Assumes the following files to be in place:
        Workflow Inputs : gs://{workspace bucket}/lapdog-executions/{submission id}/config.json
        Workflow WDL : gs://{workspace bucket}/lapdog-executions/{submission id}/method.wdl
        Submission JSON: gs://{workspace bucket}/lapdog-executions/{submission id}/submission.json

        The user will already have needed access to the workspace in order to evaluate
        workflow inputs.
        The user must have been granted access to the lapdog execution API for this
        workspace's namespace in order for the API to accept the request.
        The user's service account must have access to the workspace in order to
        download the input files specified in the workflow inputs.
        """
        warnings.warn("[ALPHA] Gateway Create Submission")
        response = requests.post(
            'https://us-central1-{project}.cloudfunctions.net/submit-alpha'.format(
                project=self.project
            ),
            headers={'Content-Type': 'application/json'},
            json={
                'token': get_access_token(),
                'bucket': bucket,
                'submission_id': submission_id,
                'namespace': self.namespace,
                'workspace': workspace,
                'workflow_options': workflow_options if workflow_options is not None else {},
                'memory': memory*1024
            }
        )
        if response.status_code == 200:
            operation = response.text
            submission_data_path = 'gs://{bucket}/lapdog-executions/{submission_id}/submission.json'.format(
                bucket=bucket,
                submission_id=submission_id
            )
            blob = getblob(submission_data_path)

            blob.upload_from_string(
                json.dumps(
                    {
                        **json.loads(blob.download_as_string().decode()),
                        **{'operation': operation}
                    }
                ).encode()
            )
            return True, operation
        return False, response


    def abort_submission(self, bucket, submission_id, hard=False):
        """
        Sends a request through the lapdog execution API to abort a running submission.
        Takes the local submission ID and a list of operations corresponding to
        the workflow calls
        Assumes the following file to be in place:
        Submission JSON: gs://{workspace bucket}/lapdog-executions/{submission id}/submission.json

        Cancels the cromwell server operation then cancels all workflow operations,
        then deletes all workflow machines, then finally the cromwell machine.
        """
        warnings.warn("[ALPHA] Gateway Abort Submission")
        response = requests.delete(
            'https://us-central1-{project}.cloudfunctions.net/abort-alpha'.format(
                project=self.project
            ),
            headers={'Content-Type': 'application/json'},
            json={
                'token': get_access_token(),
                'bucket': bucket,
                'submission_id': submission_id,
                'hard': hard
            }
        )
        if response.status_code != 200:
            return response

    def monitor_submission(self, submission_id):
        """
        Sends a request through the lapdog execution API to allow the user to connect
        to a cromwell instance to monitor the logs.
        Takes the local submission ID.
        Assumes the following file to be in place:
        Submission JSON: gs://{workspace bucket}/lapdog-executions/{submission id}/submission.json
        Requires that the user has an ssh identity set up at ~/.ssh/id_rsa

        Sends the user's public key (~/.ssh/id_rsa.pub) to the API which copies
        it to the cromwell instance
        """
        instance_ip = cache_fetch(
            'cromwell-ip',
            self.workspace.namespace,
            self.workspace.workspace,
            submission_id,
            'ip-address'
        )
        if instance_ip is None:
            # Never connected to this instance before
            if not os.path.isfile(is_rsa):
                raise FileNotFoundError("No ssh key found. Please run 'ssh-keygen'")
            print("TODO : Submit id_rsa.pub to API")
            print("TODO : Save IP address to instance_ip variable")
            cache_write(
                instance_ip,
                'cromwell-ip',
                self.workspace.namespace,
                self.workspace.workspace,
                submission_id,
                'ip-address'
            )
        return instance_ip # `ssh -i ~/.ssh/id_rsa {instance_ip}`
