"""Controllers for working with S3."""

import os

from flask import jsonify
from flask import request
from flask.views import MethodView

from helpers.auth_provider import auth_provider
from s3repo.model import ALLOWED_EXTENSIONS
from s3repo.model import S3ModelRequestError
from s3repo.package import Package


class S3Controller(MethodView):
    """Controller for working with S3 according to the REST model."""

    def __init__(self, model):
        self.model = model

    @staticmethod
    def check_filename(filename):
        """Checks if the filename corresponds the model requirements."""
        if filename == '' or not ('.' in filename and
                os.path.splitext(filename)[1] in ALLOWED_EXTENSIONS):
            return False

        return True

    @staticmethod
    def check_path(path, supported_repos):
        """Checks if the given distribution is supported for
        uploading packages.
        """
        # Correct path = repo_kind/tarantool_series/dist/dist_ver
        # Example: live/1.10/el/7
        if len(path) != 4:
            raise RuntimeError('Invalid URL.')

        if path[0] not in supported_repos['repo_kind']:
            raise RuntimeError('Repo kind "' + path[0] + '" is not supported.')
        if path[1] not in supported_repos['tarantool_series']:
            raise RuntimeError('Tarantool series "' + path[1] + '"" is not supported.')
        if path[2] not in supported_repos['distrs']:
            raise RuntimeError('Distribution "' + path[2] + '" is not supported.')
        if path[3] not in supported_repos['distrs'][path[2]]['versions']:
            raise RuntimeError('Distribution version "' + path[3] + '" is not supported.')

    @staticmethod
    def response_message(message, status):
        """Generate response with a message."""
        response = jsonify({'message': message})
        response.status_code = status
        return response

    @auth_provider.login_required
    def put(self, subpath):
        """Generates a Package object from the request and tries
        to upload it to S3 using S3Model.
        """
        package = Package()
        for _, file in request.files.items():
            if not S3Controller.check_filename(file.filename):
                msg = 'Invalid filename. Allowed file extensions: '+\
                    ', '.join(ALLOWED_EXTENSIONS)
                return S3Controller.response_message(msg, 400)

            package.add_file(file.filename, file)

        # Parse URL.
        path_list = subpath.split('/')
        try:
            S3Controller.check_path(path_list, self.model.get_supported_repos())
        except RuntimeError as err:
            return S3Controller.response_message(str(err), 400)

        # Fill in the information about the package distribution.
        package.repo_kind = path_list[0]
        package.tarantool_series = path_list[1]
        package.dist = path_list[2]
        package.dist_version = path_list[3]

        try:
            self.model.put_package(package)
        except S3ModelRequestError as err:
            return S3Controller.response_message("Can't upload the package to S3: " +
                str(err), 400)
        except Exception as err:
            return S3Controller.response_message("Can't upload the package to S3: " +
                str(err), 500)

        return S3Controller.response_message('OK', 201)

    def delete(self, subpath):
        """Delete the file or Package according to the "subpath" path."""
        return S3Controller.response_message('Delete has not yet been implemented.',
            501)
