import copy
import os
import io
import sys
import requests
from requests_toolbelt import MultipartEncoder
import mimetypes
import json
from pathlib import Path
from PIL import Image
import logging


class Data:
    def __init__(self, base_url, **kwargs):
        """Class to interact with GM-Data.

        :param base_url: URL that Data lives at. All interactions will append
            to this URL to interact with Data (ex base_url + "/self")
        :param kwargs: Extra arguments to be supplied (case insensitive):

            - USER_DN - Your USER_DN to be used for interacting with Data.
                This will be added to the header of every request.
            - logfile - File to save the log to. If not specified
            - log_level - Level of verbosity to log. Defaults to warning.
                Can be integer or string.
        """
        self.base_url = base_url
        self.headers = {}
        self.data = None
        self.hierarchy = {}
        self.log = None
        level = "warning"

        for key, value in kwargs.items():
            # print("{} is {}".format(key, value))
            if "user_dn" == key.lower():
                self.headers["USER_DN"] = value
                self.user_dn = value
            if "logfile" == key.lower():
                self.log = self.start_logger(value)
            if "log_level" == key.lower():
                level = value
        if not self.log:
            self.log = self.start_logger()
        # Set the level now that the logger exists
        self.set_log_level(level)

        try:
            self.populate_hierarchy("/", 1)
        except Exception as e:
            self.log.error("Could not populate hierarchy. Check the base_url")
            raise e

    def get_self(self):
        """Hit GM Data's self endpoint.

        :return: Description of the user's credential token in the format of
            ::

                {"label":USER_DN,"exp":1608262398,"iss":"greymatter.io",
                "values":{"email":["dave.borncamp@greymatter.io"],
                "org":["greymatter.io"]}}

        """
        r = requests.get(self.base_url + "/self", headers=self.headers)
        ret = r.text
        r.close()
        return ret

    def populate_hierarchy(self, path, oid):
        """Populate the internal hierarchy structure.

        Every GM Data data object has an Object ID, including directories and
        files. This serves as a way to keep track of individual listings
        that can be easily accessed through an API call.

        This function recursively searches the Data directory tree starting
        at the given oid and calls `list` on it.

        :param path: Directory path that the object is nestled in.
            This will be prepended to the object's name and used as a key
            in the internal hierarchy dictionary.
            Always starts out as `/` then builds to `/world` and so forth until
            the entire listing in Data is mapped.

        :param oid: Object ID of the thing to list
        """
        r = requests.get(self.base_url+"/list/{}/".format(oid),
                         headers=self.headers)
        if path == '/':
            path = ''
        for j in r.json():
            filepath = "{}/{}".format(path, j['name'])
            self.log.debug("path: {}, oid: {}".format(filepath, oid))
            self.hierarchy[filepath] = j['oid']

            # stop if it is a file
            try:
                _ = j['isfile']
                continue
            except KeyError:
                self.populate_hierarchy(filepath, j['oid'])
            r.close()

    def create_meta(self, data_filename, object_policy=None,
                    **kwargs):
        """Create the meta data for an object to be uploaded

        Will determine if the action is to create or update the object
        and create all of the necessary metadata needed for making/updating
        it.

        :param data_filename: The filename that will be used in Data
        :param object_policy: Object Policy to use. Will update an existing
            object with this value or will make a new object with this policy.
            If not supplied for either, it will make a best effort to
            come up with a good response
        :param kwargs: extra keywords to be set:
            - security - The security tag of the given file. If not supplied
            it will keep what is already there or it will use the field
            from the parent if creating a new file.
            - mimetype - Mimetype to be used as a header value to be uploaded.
            If not supplied it will make it's best guess at the value.
        :return: Metadata dictionary
        """
        self.log.debug("Create Metadata object_policy {}".format(object_policy))
        # check to see if it exists. If so, it is an update else create
        oid = self.find_file(data_filename)

        if "mimetype" in kwargs.keys():
            mimetype = kwargs["mimetype"]
        elif "local_filename" in kwargs.keys():
            mimetype = mimetypes.guess_type(kwargs["local_filename"])
        else:
            mimetype = mimetypes.guess_type(data_filename)

        # make the metadata of the upload, decide if it is an update or create
        if oid:
            self.log.debug("Found the file for updating. OID: {}".format(oid))
            r = requests.get(self.base_url+'/props/{}'.format(oid))
            meta = r.json()
            meta['action'] = "C"
            if object_policy:
                if isinstance(object_policy, str):
                    meta['objectpolicy'] = json.loads(object_policy)
                else:
                    meta['objectpolicy'] = object_policy
            try:
                if kwargs['security']:
                    meta['security'] = kwargs['security']
            except KeyError:
                pass
            r.close()
        else:
            # get the oid of the parent folder to upload into
            path = Path(data_filename)
            oid = self.find_file(str(path.parent))
            self.log.debug("New file under parent OID: {}".format(oid))
            if not oid:
                oid = self.make_directory_tree(str(path.parent),
                                               object_policy=object_policy)
            if not object_policy:
                r = requests.get(self.base_url + '/props/{}'.format(oid))
                meta = r.json()
                object_policy = meta['objectpolicy']
                r.close()
                self.log.debug("Using assumed OP {} from "
                               "oid {}".format(object_policy, oid))
            else:
                object_policy = json.loads(object_policy)
            self.log.debug("Using given OP {} from "
                           "oid {}. Type {}".format(object_policy, oid,
                                                    type(object_policy)))
            meta = {
                "action": "C",
                "name": path.name,
                "parentoid": oid,
                "isFile": True,
                "objectpolicy": object_policy,
                "mimetype": mimetype[0]
            }
            try:
                if kwargs['security']:
                    meta['security'] = kwargs['security']
            except KeyError:
                r = requests.get(self.base_url+'/props/{}'.format(oid),
                                 headers=self.headers)
                meta['security'] = r.json()['security']
                self.log.debug("Getting security: {}".format(meta['security']))
                r.close()
        return meta

    def upload_file(self, local_filename, data_filename, object_policy=None,
                    **kwargs):
        """Upload a file from the local filesystem to GM-Data.

        This will upload a file from the local file system to GM-Data.
        If the file already exists, it will update the file.
        If it does not exist, it will create a new file in the given
        directory in GM-Data.

        :param local_filename: Filename to upload on the local filesystem
        :param data_filename: Filename of the destination in GM Data
        :param object_policy: Object Policy permissions for the file to have.
            If not supplied and updating a file, it will keep what is already in
            Data. If creating a new file and not supplied, it will likely fail
            as a file will be uploaded that cannot be accessed by anyone.
        :param kwargs: extra keywords to be set:
            - security - The security tag of the given file. If not supplied
            it will keep what is already there or it will use the field
            from the parent if creating a new file.
        :return: False if request doesn't succeed or cannot be built
            True if it succeeds
        """
        self.log.debug("Uploading file {} to {} op {}".format(local_filename,
                                                              data_filename,
                                                              object_policy))
        self.log.debug("{}".format(type(object_policy)))
        mimetype = mimetypes.guess_type(local_filename)
        meta = self.create_meta(data_filename, local_filename=local_filename,
                                object_policy=object_policy, **kwargs)

        # lets get to writing! Do a multipart upload
        with open(local_filename, 'rb') as f:
            multipart_data = MultipartEncoder(
                fields={"meta": json.dumps([meta]),
                        "blob": (local_filename, f, mimetype[0])}
            )

            headers = copy.copy(self.headers)
            headers['Content-length'] = str(os.path.getsize(local_filename))
            headers['Content-Type'] = multipart_data.content_type
            r = requests.post(self.base_url+"/write", data=multipart_data,
                              headers=headers)
        self.log.debug("The sent request")
        self.log.debug("URL: {}".format(r.request.url))
        self.log.debug("Body: {}".format(r.request.body))
        self.log.debug("Headers: {}".format(r.request.headers))
        self.log.debug("Response")
        self.log.debug(r.status_code)
        self.log.debug(r.json())

        ok = r.ok
        r.close()
        if ok:
            self.hierarchy[data_filename] = r.json()[0]["oid"]

        return ok

    def make_directory_tree(self, path, object_policy=None, **kwargs):
        """Recursively create directories in GM Data.

        :param path: Path to be created in GM Data
        :param object_policy: Object Policy to be used for all folders that
            will be created
        :param kwargs: extra keywords to be set:
            - security - The security tag of the given file. If not supplied
            it will keep what is already there or it will use the field
            from the parent if creating a new file.
        :return: oid on success
        """
        path = Path(path)
        oid = self.find_file(str(path.parent))

        self.log.debug("Looking for {}, oid {}".format(path.parent, oid))
        if not oid:
            self.log.debug("Path {} not found, creating"
                           " parent".format(path.parent))
            self.make_directory_tree(str(path.parent),
                                     object_policy=object_policy,
                                     **kwargs)
        oid = self.find_file(str(path.parent))
        r = requests.get(self.base_url+'/props/{}'.format(oid))
        if not object_policy:
            object_policy = json.dumps(r.json()['objectpolicy'])

        self.log.debug("New file under parent OID: {}".format(oid))
        body = {
            "action": "U",
            "name": path.name,
            "parentoid": oid,
            "isFile": False,
            "objectpolicy": json.loads(object_policy),
        }
        if object_policy:
            body['objectpolicy'] = json.loads(object_policy)
        else:
            body['objectpolicy'] = r.json()['objectpolicy']
        r.close()
        try:
            if kwargs['security']:
                body['security'] = kwargs['security']
        except KeyError:
            r = requests.get(self.base_url + '/props/{}'.format(oid))
            body['security'] = r.json()['security']
            r.close()

        files = {
            'file': ('meta', json.dumps([body]))}
        r = requests.post(self.base_url + "/write", files=files,
                          headers=self.headers)

        self.log.debug("The sent request")
        self.log.debug("URL: {}".format(r.request.url))
        self.log.debug("Body: {}".format(r.request.body))
        self.log.debug("Headers: {}".format(r.request.headers))
        self.log.debug("Response")
        self.log.debug(r.status_code)
        self.log.debug(r.json())

        ok = r.ok
        oid = r.json()[0]["oid"]
        r.close()
        if ok:
            self.hierarchy[path] = oid

        return oid

    def get_part(self, data_filename, object_policy=None):
        """Get the file part append for a multi part file

        :param data_filename: Filename in GM Data
        :param object_policy: optional object policy to use
        :return: File part like 'aab'
        """
        part = None
        if data_filename not in self.hierarchy.keys():
            oid = self.find_file(data_filename)
            self.log.debug("Not found in hierarchy. oid {}".format(oid))
            if not oid:
                # this does not exist yet
                # yes, we want a directory named for the file
                oid = self.make_directory_tree(data_filename,
                                               object_policy=object_policy)
                part = "aaa"
                self.log.debug("File does not exist yet. oid {}".format(oid))

        else:
            # download and delete the file, rename if it is a file
            oid = self.hierarchy[data_filename]
            self.log.debug("Found in hierarchy. oid {}".format(oid))
            r = requests.get(self.base_url+'/props/{}'.format(oid),
                             headers=self.headers)
            try:
                if r.json()['isfile']:
                    self.log.debug("It's already a file, using parent's oid")
                    oid = r.json()['parentoid']
            except KeyError:
                # not a file, this is the oid we want
                pass
            r.close()
        if not part:
            # figure out the next part number
            # start by listing them off
            r = requests.get(self.base_url+"/list/{}/".format(oid),
                             headers=self.headers)
            self.log.debug("The sent request in part")
            self.log.debug("URL: {}".format(r.request.url))
            self.log.debug("Body: {}".format(r.request.body))
            self.log.debug("Headers: {}".format(r.request.headers))
            # get only the filenames
            names = [name['name'] for name in r.json() if 'isfile' in name.keys()]
            r.close()
            names.sort()
            self.log.debug("names: {}".format(names))

            # take the last one and increment it
            if len(names) == 0:
                return "aaa"
            else:
                return self._increment_str(names[-1].split(".")[0])

        return part

    def append_file(self, local_filename, data_filename, object_policy=None):
        """Append an uploaded file with another file on disk

        :param local_filename: Filename on disk that will be appended to the
            data_filename
        :param data_filename: Filename to append the new file to
        :param object_policy: Object Policy to use. Will update an existing
            object with this value or will make a new object with this policy.
            If not supplied for either, it will make a best effort to
            come up with a good response
        :return: True on success
        """
        part = self.get_part(data_filename, object_policy=object_policy)

        a = self.upload_file(local_filename, "{}/{}".format(data_filename, part),
                             object_policy=object_policy)
        return a

    def append_data(self, data, data_filename, object_policy=None):
        """Append the given filename with the given data in memory

        :param data: Data to append to a file. Remember to add line endings
            if needed.
        :param data_filename: Target filename to update
        :param object_policy: Object Policy to use. Will update an existing
            object with this value or will make a new object with this policy.
            If not supplied for either, it will make a best effort to
            come up with a good response
        :return: True on success
        """
        part = self.get_part(data_filename, object_policy=object_policy)

        mimetype = mimetypes.guess_type(data_filename)

        meta = self.create_meta("{}/{}".format(data_filename, part),
                                object_policy=object_policy,
                                mimetype=mimetype)

        if isinstance(data, str):
            with io.StringIO(data) as f:
                multipart_data = MultipartEncoder(
                    fields={"meta": json.dumps([meta]),
                            "blob": ("{}/{}".format(data_filename, part),
                                     f, mimetype[0])}
                )

                headers = copy.copy(self.headers)
                headers['Content-Type'] = multipart_data.content_type
                r = requests.post(self.base_url + "/write", data=multipart_data,
                                  headers=headers)

        else:
            with io.BytesIO(data) as f:
                multipart_data = MultipartEncoder(
                    fields={"meta": json.dumps([meta]),
                            "blob": ("{}/{}".format(data_filename, part),
                                     f, mimetype[0])}
                )

                headers = copy.copy(self.headers)
                headers['Content-Type'] = multipart_data.content_type
                r = requests.post(self.base_url + "/write", data=multipart_data,
                                  headers=headers)

        self.log.debug("The append_data sent request")
        self.log.debug("URL: {}".format(r.request.url))
        self.log.debug("Body: {}".format(r.request.body))
        self.log.debug("Headers: {}".format(r.request.headers))
        self.log.debug("Response")
        self.log.debug(r.status_code)
        self.log.debug(r.json())
        r.close()
        f.flush()

        if r.ok:
            self.hierarchy[data_filename] = r.json()[0]["oid"]

        return r.ok

    def download_file(self, file, local_filename, chunk_size=8192):
        """Downloads a file onto the local file system.

        Streams a file in chunks of 8192 to write the given file onto the
        filesystem. Streaming with chunks of this size can save lots of
        memory when downloading large files.

        :param file: File within GM-Data to download
        :param local_filename: Filename to be written onto the local filesystem
        :param chunk_size: Size of chunks to be used. Defaults to 8192
        :return: Written filename on success
        """
        oid = self.find_file(file)
        if oid:
            with requests.get(self.base_url+"/stream/{}".format(oid),
                              headers=self.headers, stream=True) as r:
                r.raise_for_status()
                with open(local_filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):

                        f.write(chunk)
            return local_filename
        else:
            self.log.warning("Cannot find file in GM-Data to download.")

    def get_buffered_steam(self, file):
        """Get a file as a data stream into memory

        :param file: File name within GM-Data to download
        :return: bytestream of file contents
        """
        oid = self.find_file(file)

        if oid:
            r = requests.get(self.base_url+"/stream/{}".format(oid),
                             headers=self.headers, stream=True)
            r.raise_for_status()
            r.raw.decode_content = True
            return io.BytesIO(r.content)
        else:
            self.log.warning("Cannot find file in GM-Data to download.")

    def stream_file(self, file):
        """Get a file loaded into memory.

        Look at the Content-Type header and parse the returned variable
        accordingly:

        - `image/jpeg` return a PIL image
        - `application/json` return a dictionary in json format
        - `text/plain` return decoded text of object

        :param file: File name within GM-Data to download
        :return: Object
        """
        oid = self.find_file(file)

        if not oid:
            self.log.warning("Cannot find file in GM-Data to download.")
            return None

        r = requests.get(self.base_url+"/stream/{}".format(oid),
                         headers=self.headers, stream=True)
        r.raise_for_status()
        r.raw.decode_content = True

        if r.headers['Content-Type'] == 'image/jpeg':
            im = Image.open(r.raw)
            return im
        if r.headers['Content-Type'] == 'application/json':
            return r.json()
        if r.headers['Content-Type'] == 'text/plain':
            return r.content.decode()

    # --- Utility functions

    def find_file(self, filename):
        """Find a given file within the file hierarchy

        Try to find a file within the file hierarchy, if it is not immediately
        found, repopulate the hierarchy and try again. If it is still
        not found, return None

        :param filename: Filename to be fond within GM Data
        :return: The GM Data oid if found or None if not
        """
        try:
            oid = self.hierarchy[filename]
            return oid
        except KeyError:
            # Maybe it got populated since this class last checked
            self.populate_hierarchy("/", 1)

        try:
            oid = self.hierarchy[filename]
            return oid
        except KeyError:
            return None

    @staticmethod
    def start_logger(name="pygmdata", logfile=None):
        """Start logging what is going on

        :param name: Name of the logger to use. Defaults to "pygmdata"
        :param logfile: Name of output logfile. Default to not saving.
        :return: logfile written to disk
        """
        log = logging.getLogger(name)

        fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        # create the logging file handler
        logging.basicConfig(filename=logfile, format=fmt)

        # -- handler for STDOUT
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter(fmt)
        ch.setFormatter(formatter)
        logging.getLogger().addHandler(ch)

        return log

    def set_log_level(self, level):
        """ Set the log level for the log

        :param level: Level of verbosity to log. Defaults to warning.
            Can be integer or string.
        :return: None
        """
        if isinstance(level, int):
            self.log.setLevel(level)
            return

        if level.lower() == "info":
            self.log.setLevel(logging.getLevelName('INFO'))
        elif level.lower() == 'debug':
            self.log.setLevel(logging.getLevelName('DEBUG'))
        elif level.lower() == 'warning':
            self.log.setLevel(logging.getLevelName('WARNING'))
        elif level.lower() == 'error':
            self.log.setLevel(logging.getLevelName('ERROR'))

    @staticmethod
    def _increment_char(c):
        """
        Increment an uppercase character, returning 'a' if 'z' is given
        """
        return chr(ord(c) + 1) if c != 'z' else 'a'

    def _increment_str(self, s):
        lpart = s.rstrip('z')
        num_replacements = len(s) - len(lpart)
        new_s = lpart[:-1] + self._increment_char(lpart[-1]) if lpart else 'a'
        new_s += 'a' * num_replacements
        return new_s
