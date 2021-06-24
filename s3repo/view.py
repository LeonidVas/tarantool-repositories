"""View for working with the repositories on S3."""

import os

from flask.views import View
from flask import jsonify
from flask import render_template
from flask import Response


class S3View(View):
    """View for working with S3 according to the REST model."""

    def __init__(self, model):
        self.model = model

    @staticmethod
    def response_message(message, status):
        """Generate response with a message."""
        response = jsonify({'message': message})
        response.status_code = status
        return response

    @staticmethod
    def get_directory(abs_path, rel_path, bucket_name, items):
        """Display directory content as a HTML page."""
        displayed_path = os.path.normpath('/'.join(['', bucket_name, abs_path]))
        parent_path = '/'.join(rel_path.split('/')[:-2])

        # Make size human readable.
        readable_items = []
        for item in items:
            if item.Size != '':
                readable_size = ''
                size = int(item.Size)
                for unit in ['', 'Ki', 'Mi', 'Gi']:
                    if abs(size) < 1024.0:
                        readable_size = "%3.1f %sB" % (size, unit)
                        break
                    size /= 1024.0
                item = item._replace(Size=readable_size)
            readable_items.append(item)

        list_parameters = {'displayed_path': displayed_path,
                           'path': rel_path,
                           'parent_path': parent_path,
                           'items': readable_items}
        return render_template('index.html', **list_parameters)

    @staticmethod
    def get_file(path, responce):
        """Download a file to user's machine."""
        filename = path.split('/')[-1]
        return Response(
            responce['Body'].read(),
            mimetype='application/octet-stream',
            headers={"Content-Disposition": "attachment; filename=" + filename}
            )

    def dispatch_request(self, subpath='/', type='directory'):
        """Show a directory or download a file according to the
        "subpath" path.
        """
        rel_path = '' if subpath.strip('/') == 'index' else subpath
        base_path = self.model.s3_settings.get('base_path')
        abs_path = os.path.normpath('/'.join([base_path, rel_path]))
        abs_path = abs_path.strip('/')

        err_msg = ''
        try:
            if type == 'directory':
                if abs_path != '':
                    abs_path = abs_path + '/'
                items = self.model.get_directory(abs_path)
                err_msg = "Can't show the directory in S3: "
                return S3View.get_directory(abs_path, rel_path,
                                            self.model.bucket.name, items)
            elif type == 'file':
                responce = self.model.get_file(abs_path)
                err_msg = "Can't download file from S3: "
                return S3View.get_file(abs_path, responce)
            else:
                return S3View.response_message('Wrong URL.', 404)
        except RuntimeError as err:
            return S3View.response_message(err_msg + str(err), 500)
