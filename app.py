"""The Repository Web Service is designed to interact with the repository via HTTP."""

import json
import os

from flask import Flask

from helpers.auth_provider import auth_provider
from s3repo.model import S3AsyncModel
from s3repo.controller import S3Controller
from s3repo.view import S3View


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


def server_prepare():
    """Prepare server for run."""
    # Get configuration.
    cfg = load_cfg()
    update_cfg_by_env(cfg)

    # Configure the auth module.
    auth_provider.set_credentials(cfg['common']['credentials'])

    # Configure S3 backend.
    s3_model = S3AsyncModel(cfg['model'])
    s3_model.sync_all_repos()

    # Set the controller to work with S3.
    s3_controller = S3Controller.as_view('s3_controller', s3_model)
    app.add_url_rule('/<path:subpath>', view_func=s3_controller,
        methods=['PUT', 'DELETE'])
    # Set the view to work with S3.
    s3_view = S3View.as_view('s3_view', s3_model)
    app.add_url_rule('/', view_func=s3_view, methods=['GET'])
    app.add_url_rule('/<path:subpath>', view_func=s3_view, methods=['GET'])
    app.add_url_rule('/<path:subpath>?<string:type>', view_func=s3_view,
                     methods=['GET'])


app = Flask(__name__)
server_prepare()
