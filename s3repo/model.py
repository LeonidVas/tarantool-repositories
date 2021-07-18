"""Model for working with the repositories on S3."""

from multiprocessing.pool import ThreadPool
import os
import re
import subprocess as sp
import tempfile
import time
from collections import namedtuple
from threading import Lock
from threading import Thread

import boto3


ALLOWED_EXTENSIONS = {'.rpm', '.deb', '.dsc', '.xz', '.gz'}


class S3ModelRequestError(Exception):
    """S3ModelRequestError - exception that is raised when trying to
    use invalid input data when working with any S3Model.
    """


class S3AsyncModel:
    """S3AsyncModel - model for working with repositories
    on S3 in "Async" mode. "Async" means that it has several
    independent steps:
        - add / update / delete a package to the repository
        - update the repository metainformation

    If any other actions are performed on the packages between
    adding the package and updating the metainformation, this
    also will be reflected in the metainformation.

    The "mkrepo" util (https://github.com/knazarov/mkrepo)
    will be used to update metainformation.
    """

    def __init__(self, s3_settings):
        """When the "S3AsyncModel" object is created, a resource
        representing the S3 segment is created and the synchronization
        thread is started. A sync thread is required to update
        metainformation in updated repositories.

        s3_settings - dictionary contains the settings required
        to connect to S3.
        s3_settings:
            - region - S3 region
            - endpoint_url - S3 server URL
            - bucket_name - name of a bucket with repositories
            - base_path - path inside the bucket to repositories
            - access_key_id - S3 access key ID
            - secret_access_key - S3 secret key
            - supported_repos - dictionary describing the supported
                repositories, tarantool version, distributions...
        """
        self.s3_settings = s3_settings
        self.s3_resource = boto3.resource(
            service_name='s3',
            region_name=self.s3_settings['region'],
            endpoint_url=self.s3_settings['endpoint_url'],
            aws_access_key_id=self.s3_settings['access_key_id'],
            aws_secret_access_key=self.s3_settings['secret_access_key']
        )
        self.bucket = self.s3_resource.Bucket(self.s3_settings['bucket_name'])
        self.s3_client = self.bucket.meta.client

        # unsync_repos - set of repositories for which metainformation
        # needs to be updated. All actions with "unsync_repos" must
        # be done under the "sync_lock".
        self.sync_lock = Lock()
        self.unsync_repos = set()

        # A sync thread is required to update metainformation
        # in updated repositories.
        self.sync_thread = Thread(target=self.sync, args=(True,))
        self.sync_thread.daemon = True
        self.sync_thread.start()

    @staticmethod
    def _format_paths(dist_path, dist_version, dist_base, filename):
        """Formats the file path and repository path according
        to the filename and distribution information.
        Returns a tuple (repo_path, path).
        """
        path = ''
        repo_path = ''
        file_type_err = 'The "{0}" file does not match the type of files ' +\
            'used in the {1}-based repositories.'
        if dist_base == 'rpm':
            if re.fullmatch(r'.*\.(x86_64|noarch)\.rpm', filename):
                # Example of the path for x86_64, noarch rpm repository:
                # .../live/1.10/fedora/31/x86_64
                repo_path = '/'.join([
                    dist_path,
                    dist_version,
                    'x86_64'
                ])
                # Example of the path to upload rpm files:
                # .../live/1.10/fedora/31/x86_64/Packages
                path = '/'.join([
                    repo_path,
                    'Packages',
                    filename
                ])
            elif re.fullmatch(r'.*\.src\.rpm', filename):
                # Example of the path for src.rpm repository:
                # .../live/1.10/fedora/31/SRPMS
                repo_path = '/'.join([
                    dist_path,
                    dist_version,
                    'SRPMS'
                ])
                # Example of the path to upload src.rpm files:
                # .../live/1.10/fedora/31/SRPMS/Packages
                path = '/'.join([
                    repo_path,
                    'Packages',
                    filename
                ])
            else:
                raise S3ModelRequestError(file_type_err.format(
                    filename, dist_base))
        elif dist_base == 'deb':
            if re.fullmatch(r'.*\.(deb|dsc|tar\.xz|tar\.gz)', filename):
                # https://wiki.debian.org/DebianRepository/Format
                # Example of the path for deb repository ("archive root"):
                # .../live/1.10/ubuntu
                repo_path = dist_path
                # Example of the path to upload files:
                # .../live/1.10/ubuntu/pool/disco/main/s/small
                path = '/'.join([
                    repo_path,
                    'pool',
                    dist_version,
                    'main',
                    filename[:1],
                    filename.partition('_')[0],
                    filename
                ])
            else:
                raise S3ModelRequestError(file_type_err.format(
                    filename, dist_base))
        else:
            raise RuntimeError('Unknown repository base: {0}.'.format(dist_base))

        return (repo_path, path)

    @staticmethod
    def _list_items(objects):
        """Process "objects" from list_objects_v2() and certain
        them to items list. "object" is a list having certain
        structure definded as a returned value of
        list_objects_v2().
        """

        fields = ['Type', 'Name', 'LastModified', 'Size']

        # Item is tuple with metainfo of file or directory.
        # Fields format:
        # +------+------+--------------+------+
        # | Type | Name | LastModified | Size |
        # +------+------+--------------+------+
        Item = namedtuple('Item', fields, defaults=['', '', '', ''])

        # KeyCount is the number of keys returned with this
        # request. KeyCount is 1 or emtpy directory (this folder
        # itself as a key). Zero KeyCount means that this
        # requested directory (prefix) doesn't exist.
        if not objects.get('KeyCount'):
            raise RuntimeError('No such directory.')

        # Actually, S3 doesn't use the term directory/path, it
        # simply maps the objects inside the bucket to a key like
        # "path/to/object", where "/" is used as a delimiter for
        # the common prefix of a group of keys. The result returns
        # each distinct key prefix containing the delimiter in a
        # "CommonPrefixes" element i.e. list of subdirectories.
        common_prefixes = objects.get('CommonPrefixes') or {}
        items = []
        for prefix in common_prefixes:

            directory_name = prefix.get('Prefix').split('/')[-2]
            item = Item('directory', directory_name)
            items.append(item)

        # "Contents" is a metadata about each file returned.
        contents = objects.get('Contents') or {}
        for file_meta in contents:
            file_name = file_meta.get('Key').split('/')[-1]
            # list_objects_v2() paginator returns the current
            # directory, as well as files with the same name
            # (prefix), messing with the current directory,
            # because they are S3 objects too. Ignore these
            # objects.
            if not file_name:
                continue
            last_modified = file_meta.get('LastModified').strftime("%Y-%m-%d %H:%M:%S")
            size = file_meta.get('Size')

            item = Item('file', file_name, last_modified, size)
            items.append(item)

        return items

    def _upload_files(self, package, tarantool_series, origin_files):
        """Upload files to one repo on S3."""
        # self.s3_settings['base_path'] can be None or '', in this case,
        # you do not need to add it to the path.
        dist_path_list = [package.repo_kind, tarantool_series, package.dist]
        if self.s3_settings.get('base_path', ''):
            dist_path_list.insert(0, self.s3_settings['base_path'])

        dist_path = '/'.join(dist_path_list)
        dist_base = self.get_supported_repos()['distrs'][package.dist]['base']

        # List of repositories where the new package has been uploaded,
        # but the metainformation hasn't been updated yet.
        unsync_repos_local = set()
        for filename, file in package.files.items():
            repo_path, path = S3AsyncModel._format_paths(
                dist_path, package.dist_version, dist_base, filename)

            # If a file needs to be uploaded to several repositories:
            # it is uploaded to one of them, and then copied to others.
            if filename in origin_files:
                self.bucket.copy(origin_files[filename], path)
            else:
                obj = self.bucket.Object(path)
                obj.upload_fileobj(file)
                origin_files[filename] = {
                    'Bucket': self.bucket.name,
                    'Key': path
                }

            # Several files can be uploaded to the same repo.
            # Let's add the repo to the local "unsync_repos" set
            # and merge it with the global one after the end of
            # the iteration..
            unsync_repos_local.add(repo_path)

        self.sync_lock.acquire()
        self.unsync_repos.update(unsync_repos_local)
        self.sync_lock.release()

    def _get_deb_repo_path(self, base_path):
        """Returns the path (as list) to a deb-based repository
        for updating metainformation with the 'mkrepo' tool.
        base_path(string) - path to the distribution.
        """

        # Actually, S3 doesn't use the term directory/path, it simply maps the
        # objects inside the bucket to a key like "path/to/object", where "/"
        # is used as a delimiter for the common prefix of a group of keys.
        # Here the term "path" is used to analogy with navigation in a local
        # file system such as "ext4". Since we want to known if the repository
        # contains files, we request if objects with this prefix("path") exist
        # (it is enough for us to know that the first level content is exists).
        #
        # See https://github.com/boto/boto3/issues/134 for how to list first
        # level content by a specific prefix.

        path = base_path + '/'
        list_objs = self.bucket.meta.client.list_objects_v2(
            Bucket=self.bucket.name,
            Delimiter='/',
            Prefix=path
        )
        if list_objs.get('CommonPrefixes') is None:
            return []

        # In the case of a deb-base distribution, the meta information about
        # packages in all versions of the distribution is updated together.
        # So we just return the path to the distribution
        return [path]

    def _get_rpm_repo_path(self, base_path, dist_versions):
        """Returns the list of the paths to the rpm-based reposies
        for updating metainformation with the 'mkrepo' tool.
        base_path(string) - path to the distribution.
        dist_versions(list) - list of the distribution versions.
        """
        # See the first comment in "_get_deb_repo_path" method.

        # In the case of an rpm-based distribution, one distribution version can
        # include several repositories (for example "x86_64" and "SRPM").
        # We must collect them all.
        repos_list = []
        for ver in dist_versions:
            # Path to all repositories of the distribution version.
            common_path = '/'.join([base_path, ver]) + '/'
            list_objs = self.bucket.meta.client.list_objects_v2(
                Bucket=self.bucket.name,
                Delimiter='/',
                Prefix=common_path
            )
            dist_repos_list = list_objs.get('CommonPrefixes')
            if dist_repos_list is None:
                continue
            for repo in dist_repos_list:
                # In this context, "Prefix" is a path to the repository.
                repos_list.append(repo['Prefix'])

        return repos_list

    def _get_repository_list(self):
        """Returns a list of paths to repositories in the current bucket."""
        supported_repos = self.get_supported_repos()
        repos_list = []
        result_list = []

        # Since collecting the list of paths to repositories involves a large
        # number of S3 requests through the network, it is recommended to use a
        # thread pool to execute them in parallel (the thread will be idle for a
        # "long" time, waiting for a response from S3).
        #
        # The number of processes = 20 was chosen experimentally.
        with ThreadPool(processes=20) as pool:
            for kind in supported_repos['repo_kind']:
                for series in supported_repos['tarantool_series']:
                    for dist, dist_description in supported_repos['distrs'].items():
                        path = ''
                        base_path = self.s3_settings.get('base_path', '')
                        if base_path:
                            path = '/'.join([base_path, kind, series, dist])
                        else:
                            path = '/'.join([kind, series, dist])
                        if dist_description['base'] == 'deb':
                            result_list.append(
                                pool.apply_async(self._get_deb_repo_path, (path,))
                            )
                        elif dist_description['base'] == 'rpm':
                            result_list.append(
                                pool.apply_async(
                                    self._get_rpm_repo_path,
                                    (path, dist_description['versions'])
                                )
                            )
                        else:
                            raise RuntimeError('Unknown repository base: ' +
                                               dist_description['base'])

            # Collect the information about location of all the
            # repositories from the bucket together.
            for res in result_list:
                repos_list.extend(res.get())

        return repos_list

    def get_supported_repos(self):
        """Get description of the currently supported repos."""
        return self.s3_settings['supported_repos']

    def sync_all_repos(self):
        """Update the metainformation of all known repositories."""
        # Get the information about location of all the
        # repositories from the bucket.
        repos_to_update = self._get_repository_list()

        # Add the repositories to the unsync list.
        self.sync_lock.acquire()
        for repo in repos_to_update:
            self.unsync_repos.add(repo)
        self.sync_lock.release()

        # Add additional workers to update metainformation (approximate
        # number of repositories to be synced ~ 600).
        # 20 - the number up on the spot. Perhaps it will be corrected later.
        threads_num = 20
        with ThreadPool(processes=threads_num) as pool:
            result_list = []
            for _ in range(0, threads_num):
                result_list.append(pool.apply_async(self.sync, (False,)))
            for res in result_list:
                # Wait for all additional workers to complete.
                res.wait()

    def sync(self, permanent):
        """Update a metainformation of repositoties from the "unsync_repo" set.
        permanent(bool) - describes whether the function should process data
        permanent or whether it can "return" if all current work has been
        completed.
        """
        while True:
            if self.unsync_repos:
                self.sync_lock.acquire()
                sync_repo = self.unsync_repos.pop()
                self.sync_lock.release()

                with tempfile.TemporaryDirectory(prefix='.rws_', dir='.') as tmpdirname:
                    mkrepo_cmd = [
                        'mkrepo',
                        '--temp-dir',
                        tmpdirname,
                        '--s3-access-key-id',
                        str(self.s3_settings['access_key_id']),
                        '--s3-secret-access-key',
                        str(self.s3_settings['secret_access_key']),
                        '--s3-endpoint',
                        str(self.s3_settings['endpoint_url']),
                        '--s3-region',
                        str(self.s3_settings['region']),
                    ]

                    # Include the package metainformation signature
                    # if we have a gpg key.
                    env = None
                    if self.s3_settings.get('gpg_sign_key'):
                        mkrepo_cmd.append('--sign')
                        env = dict(os.environ,
                                   GPG_SIGN_KEY=self.s3_settings['gpg_sign_key'])

                    # Set the path to the repository.
                    mkrepo_cmd.append('s3://{0}/{1}'.format(
                        self.s3_settings['bucket_name'],
                        sync_repo))

                    with sp.Popen(mkrepo_cmd, env=env) as mkrepo_ps:
                        result = mkrepo_ps.wait()
                        if result != 0:
                            self.sync_lock.acquire()
                            self.unsync_repos.add(sync_repo)
                            self.sync_lock.release()
            elif permanent:
                # The "unsync_repos" set is empty.
                # Let's just wait a while.
                time.sleep(5)
            else:
                # This is a temporary "worker" and all current
                # work has been completed.
                break

    def put_package(self, package):
        """Load the package to S3."""
        tarantool_series_to_upload = []
        if package.tarantool_series == 'enabled':
            tarantool_series_to_upload = self.get_supported_repos()['enabled']
        else:
            tarantool_series_to_upload.append(package.tarantool_series)

        # Files already uploaded to S3.
        # Information from this dict is used to copy a file from
        # one repository to another if the file is already uploaded to S3.
        origin_files = {}

        for tarantool_series in tarantool_series_to_upload:
            self._upload_files(package, tarantool_series, origin_files)

    def get_package(self, package):
        """Download a package from S3."""
        NotImplementedError("get_package hasn't been implemented yet.")

    def delete_package(self, package):
        """Delete a package from S3."""
        NotImplementedError("delete_package hasn't been implemented yet.")

    def get_directory(self, path):
        """Get lists and metadata of directories and files within
        directory from S3.
        """

        paginator = self.s3_client.get_paginator('list_objects_v2')
        # Parameters for list_objects_v2():
        # * 'Bucket' is a bucket name.
        # * 'Delimiter' is a character you use to group keys.
        # * 'Prefix' limits the response to keys that begin with
        # the specified prefix i.e. it allows to get files and
        # subdiectories located only in directory specified by
        # 'path'.
        #
        # See https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.list_objects_v2
        # for more detaied description.
        list_parameters = {'Bucket': self.bucket.name,
                           'Delimiter': '/',
                           'Prefix': path}

        items = []
        for objects in paginator.paginate(**list_parameters):
            items.extend(S3AsyncModel._list_items(objects))

        return items

    def get_file(self, path):
        """Get a file from S3 as a "Responce' object."""

        self.sync_lock.acquire()
        try:
            # We suppose that files which we deal with are not
            # large enough.
            responce = self.s3_client.get_object(Bucket=self.bucket.name,
                                                 Key=path)
        except self.s3_client.exceptions.NoSuchKey:
            self.sync_lock.release()
            raise RuntimeError("No such key.")
        except self.s3_client.exceptions.InvalidObjectState:
            self.sync_lock.release()
            raise RuntimeError("Invalid object state.")
        self.sync_lock.release()
        return responce

    def delete_file(self, path):
        """Delete a file from S3."""
        NotImplementedError("delete_file hasn't been implemented yet.")
