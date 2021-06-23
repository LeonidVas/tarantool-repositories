"""The Repository Web Service is designed to interact with the repository via HTTP."""

import json
import logging
import os
import re
import subprocess as sp

from flask import Flask

from helpers.auth_provider import auth_provider
from s3repo.model import S3AsyncModel
from s3repo.controller import S3Controller


def load_cfg():
    """Load and parse the config."""
    # Get path to config from env and check if it exists.
    env_cfg_path = os.getenv('RWS_CFG')
    if env_cfg_path is None or not os.path.isfile(env_cfg_path):
        raise RuntimeError('Configuration file does not exist.')

    # Parse config.
    cfg = {}
    with open(env_cfg_path) as cfg_file:
        cfg = json.load(cfg_file)

    return cfg


def add_gpg_key(gpg_key):
    """Adds the given keys to the keyring."""
    # Add keys to the keyring.
    cmd = ['gpg', '--batch', '--import']
    stdout = None
    with sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.STDOUT) as proc:
        stdout, _ = proc.communicate(input=gpg_key)
        stdout = stdout.decode('utf-8')
        if proc.returncode != 0:
            raise RuntimeError('Can not add gpg key: "{0}".'.format(stdout))

    # Get name of the key from output.
    match = re.search(r'gpg: key (?P<name>[0-9A-F]{16}): secret key imported',
                      stdout)
    if not match:
        raise RuntimeError('Can not get name of the key.')

    return match.group('name')


def update_cfg_by_env(cfg):
    """Update the config with data from environment variables."""
    # Get some configuration parameters from env.
    env_model_settings = {}
    env_model_settings['region'] = os.getenv('S3_REGION')
    env_model_settings['endpoint_url'] = os.getenv('S3_URL')
    env_model_settings['bucket_name'] = os.getenv('S3_BUCKET')
    env_model_settings['base_path'] = os.getenv('S3_BASE_PATH')
    env_model_settings['access_key_id'] = os.getenv('S3_ACCESS_KEY')
    env_model_settings['secret_access_key'] = os.getenv('S3_SECRET_KEY')
    gpg_key_armored = os.getenv('GPG_SIGN_KEY_ARMORED')
    # GPG_SIGN_KEY_ARMORED stores GPG secret key for signing the repositories
    # metadata. Our task is to add this key to the system and get its name.
    if gpg_key_armored:
        env_model_settings['gpg_sign_key'] = \
            add_gpg_key(gpg_key_armored.encode('ascii'))

    env_common_settings = {}
    env_common_settings['credentials'] = \
        json.loads(os.environ.get('RWS_CREDENTIALS'))

    # Check if credentials are set for at least one user.
    if len(env_common_settings['credentials']) < 1:
        RuntimeError('No credentials have been set for at least one user.')

    # Populate the config with data from environment variables.
    for item in env_model_settings.items():
        if item[1]:
            cfg['model'][item[0]] = item[1]

    if cfg.get('common') is None:
        cfg['common'] = {}
    for item in env_common_settings.items():
        if item[1]:
            cfg['common'][item[0]] = item[1]


def logging_cfg():
    """Configure logging."""
    logging.basicConfig(format='%(asctime)s (%(levelname)s) %(message)s',
                        level=logging.INFO)


def server_prepare():
    """Prepare server for run."""
    # Get configuration.
    logging.info('Load cfg...')
    cfg = load_cfg()
    update_cfg_by_env(cfg)

    # Configure the auth module.
    logging.info('Configure auth module...')
    auth_provider.set_credentials(cfg['common']['credentials'])

    # Configure S3 backend.
    s3_model = S3AsyncModel(cfg['model'])
    logging.info('Synchronizing metainformation of repositories...')
    s3_model.sync_all_repos()

    # Set the controller to work with S3.
    logging.info('Set handlers...')
    s3_view = S3Controller.as_view('s3_view', s3_model)
    app.add_url_rule('/<path:subpath>', view_func=s3_view,
        methods=['GET', 'PUT', 'DELETE'])


# It is a good practice to configure logging
# before creating the application object.
# (https://flask.palletsprojects.com/en/2.0.x/logging/#basic-configuration)
logging_cfg()
app = Flask(__name__)
server_prepare()
logging.info('Start server...')
