import logging
import os
import sys
import json
import shutil
import csv
import cherrypy
import re
import time
import datetime
import StringIO # For converting KV store array data to CSV for export
import cgi # For escaping lookup contents

from splunk import AuthorizationFailed, ResourceNotFound
import splunk.rest
import splunk.appserver.mrsparkle.controllers as controllers
import splunk.appserver.mrsparkle.lib.util as util
import splunk.entity as entity
from splunk.appserver.mrsparkle.lib import jsonresponse
from splunk.appserver.mrsparkle.lib.util import make_splunkhome_path
from splunk.appserver.mrsparkle.lib.decorators import expose_page

bin_dir = os.path.join(util.get_apps_dir(), __file__.split('.')[-2], 'bin')

if not bin_dir in sys.path:
    sys.path.append(dir)

import lookupfiles

# The default of the csv module is 128KB; upping to 10MB. See SPL-12117 for 
# the background on issues surrounding field sizes. 
# (this method is new in python 2.5) 
csv.field_size_limit(10485760)

def setup_logger(level):
    """
    Setup a logger for the REST handler.
    """

    logger = logging.getLogger('splunk.appserver.lookup_editor.controllers.LookupEditor')
    logger.propagate = False # Prevent the log messages from being duplicated in the python.log file
    logger.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(make_splunkhome_path(['var', 'log', 'splunk', 'lookup_editor_controller.log']), maxBytes=25000000, backupCount=5)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger

logger = setup_logger(logging.INFO)

from splunk.models.base import SplunkAppObjModel
from splunk.models.field import BoolField, Field

"""
Represents an exception when the user did not have sufficient permissions.
"""
class PermissionDeniedException(Exception):
    pass

"""
Represents an exception when the user requested a lookup file that was too big.
"""
class LookupFileTooBigException(Exception):
    def __init__(self, file_size):

        # Call the base class constructor with the parameters it needs
        super(LookupFileTooBigException, self).__init__("Lookup file is too large to be loaded")

        # Remember the file-size
        self.file_size = file_size

"""
Provides a model for retrieving the list of apps from Splunk.
"""
class App(SplunkAppObjModel):
    ''' Represents a Splunk app '''
    
    resource      = 'apps/local'
    is_disabled   = BoolField('disabled')
    is_configured = BoolField('configured')
    label         = Field()
    
def isEmpty( row ):
    """
    Determines if the given row in a lookup is empty. This is done in order to prune rows that are empty.
    """
    
    for e in row:
        if e is not None and len(e.strip()) > 0:
            return False
        
    return True

def force_lookup_replication(app, filename, sessionKey, base_uri=None):
    """
    Force replication of a lookup table in a Search Head Cluster.
    """

    # Permit override of base URI in order to target a remote server.
    endpoint = '/services/replication/configuration/lookup-update-notify'
    
    if base_uri:
        repl_uri = base_uri + endpoint
    else:
        repl_uri = endpoint
        
    # Provide the data that describes the lookup
    payload = {
               'app': app,
               'filename': os.path.basename(filename),
               'user': 'nobody'
    }
    
    # Perform the request
    response, content = splunk.rest.simpleRequest(repl_uri, 
        method='POST', 
        postargs=payload, sessionKey=sessionKey, raiseAllErrors=False)

    # Analyze the response
    if response.status == 400:
        if 'No local ConfRepo registered' in content:
            # search head clustering not enabled
            logger.info('Lookup table replication not applicable for %s: clustering not enabled', filename)
            return (True, response.status, content)
        elif 'Could not find lookup_table_file' in content:
            logger.error('Lookup table replication failed for %s: status_code="%s", content="%s"', filename, response.status, content)
            return (False, response.status, content)
        else:
            # Previously unforeseen 400 error.
            logger.error('Lookup table replication failed for %s: status_code="%s", content="%s"', filename, response.status, content)
            return (False, response.status, content)
        
    elif response.status != 200:
        return (False, response.status, content)
    
    # Return a default response
    logger.info('Lookup table replication forced for %s', filename)
    return (True, response.status, content)

class LookupEditor(controllers.BaseController):
    '''
    Lookup Editor Controller
    '''
 
    MAXIMUM_EDITABLE_SIZE = 10 * 1024 * 1024 # 10 MB
 
    @staticmethod
    def getCapabilities4User(user=None, session_key=None):
        """
        Get the capabilities for the given user.
        """
        
        roles = []
        capabilities = []
        
        # Get user info              
        if user is not None:
            logger.info('Retrieving role(s) for current user: %s' % (user))
            userDict = entity.getEntities('authentication/users/%s' % (user), count=-1, sessionKey=session_key)
        
            for stanza, settings in userDict.items():
                if stanza == user:
                    for key, val in settings.items():
                        if key == 'roles':
                            logger.info('Successfully retrieved role(s) for user: %s' % (user))
                            roles = val
             
        # Get capabilities
        for role in roles:
            logger.info('Retrieving capabilities for current user: %s' % (user))
            roleDict = entity.getEntities('authorization/roles/%s' % (role), count=-1, sessionKey=session_key)
            
            for stanza, settings in roleDict.items():
                if stanza == role:
                    for key, val in settings.items():
                        if key == 'capabilities' or key =='imported_capabilities':
                            logger.info('Successfully retrieved %s for user: %s' % (key, user))
                            capabilities.extend(val)
            
        return capabilities     
    
    @expose_page(must_login=True, methods=['GET']) 
    def get_lookup_info(self, lookup_file, namespace="lookup_editor", **kwargs):
        """
        Get information about a lookup file
        """

        logger.info("Retrieving information about a lookup file...")
        
        user = cherrypy.session['user']['name']
        session_key = cherrypy.session.get('sessionKey')
        
        # Load defaults (cherrypy won't let me assign defaults in the function definition)
        owner = kwargs.get('owner', None)
        version = kwargs.get('version', None)
        
        # Ensure that the file name is valid
        if not self.is_file_name_valid(lookup_file):
            cherrypy.response.status = 400
            return self.render_error_json(_("The lookup filename contains disallowed characters"))
        
        # Get a reference to the file
        full_lookup_filename = self.resolve_lookup_filename(lookup_file, namespace, owner, get_default_csv=True, version=version)
        
        # Below is the description of the file
        desc = {}
        
        # Fill out information about this file
        desc['filename'] = full_lookup_filename
        
        # Get the size of the file
        try:
            file_size = os.path.getsize(full_lookup_filename)
            desc['size'] = file_size
            desc['is_too_big_for_editing'] = (file_size > LookupEditor.MAXIMUM_EDITABLE_SIZE)
            
        except os.error:
            cherrypy.response.status = 400
            return self.render_error_json(_("The lookup file could not be opened"))
        
        # Return the information
        return self.render_json(desc)
        
        
    def is_file_name_valid(self, lookup_file):     
        """
        Indicate if the lookup file is valid (doesn't contain invalid characters such as "..").
        """
         
        allowed_path = re.compile("^[-A-Z0-9_ ]+([.][-A-Z0-9_ ]+)*$", re.IGNORECASE)
        
        if not allowed_path.match(lookup_file):
            return False
        else:
            return True
        
    def getBackupFiles(self, lookup_file, namespace, owner):
        """
        Get a list of backup files for a given file
        """
        
        # Escape the file name so that we find the correct file
        escaped_filename = self.escapeFilename(lookup_file)
        
        # Get the backup directory and determine the path to the backups
        backup_directory = self.getBackupDirectory(escaped_filename, namespace, owner)
        
        # Get the backups
        backups = [ f for f in os.listdir(backup_directory) if os.path.isfile(os.path.join(backup_directory,f)) ]
        
        return backups
    
    @expose_page(must_login=True, methods=['GET']) 
    def get_lookup_backups_list(self, lookup_file, namespace, owner=None, **kwargs):
        
        backups = self.getBackupFiles(lookup_file, namespace, owner)
        
        # Make the response
        backups_meta = []
        
        for backup in backups:
            try:
                backups_meta.append(
                                    {
                                     'time': backup,
                                     'time_readable' : datetime.datetime.fromtimestamp(float(backup)).strftime('%Y-%m-%d %H:%M:%S')
                                    }
                                    )
            except ValueError:
                logger.warning("Backup file name is invalid, file_name=%s", backup)
        
        # Sort the list
        backups_meta = sorted(backups_meta, key=lambda x: float(x['time']), reverse=True)
        
        return self.render_json(backups_meta, set_mime='application/json')
        
    def escapeFilename(self, file_name):
        """
        Return a file name the excludes special characters (replaced with underscores)
        """
        
        return re.sub(r'[/\\?%*:|"<>]', r'_', file_name)
    
    def getBackupDirectory(self, lookup_file, namespace, owner=None, resolved_lookup_path=None):
        """
        Get the backup directory where the lookup should be stored
        """
        
        if owner is None:
            owner = 'nobody'
        
        # Identify the current path of the given lookup file
        if resolved_lookup_path is None:
            resolved_lookup_path = lookupfiles.SplunkLookupTableFile.get(lookupfiles.SplunkLookupTableFile.build_id(lookup_file, namespace, owner)).path
        
        # Determine what the backup directory should be
        backup_directory = make_splunkhome_path([os.path.dirname(resolved_lookup_path), "lookup_file_backups", namespace, owner, self.escapeFilename(lookup_file)])
        #backup_directory = make_splunkhome_path([os.path.split(resolved_lookup_path)[0], "lookup_file_backups", namespace, owner, self.escapeFilename(lookup_file)])
        
        # Make the backup directory, if necessary
        if not os.path.exists(backup_directory):
            os.makedirs(backup_directory)
        
        logger.debug("Backup directory is:" + backup_directory)
            
        return backup_directory
                
    def backupLookupFile(self, lookup_file, namespace, owner=None, resolved_file_path=None):
        """
        Make a backup if the lookup file
        """
        
        try:
            
            # If we don't already know the path of the file, then load it
            if resolved_file_path is None:
                resolved_file_path = self.resolve_lookup_filename(lookup_file, namespace, owner, throw_not_found=False)
                
            # If the file doesn't appear to exist yet. Then skip the backup.
            if resolved_file_path is None:
                logger.info("The file dosen't exist yet; the backup will not be made")
                return None
            
            # Get the backup directory
            backup_directory = self.getBackupDirectory(lookup_file, namespace, owner, resolved_lookup_path=resolved_file_path)
            
            # Get the date of the existing file so that we put the 
            try:
                file_time = os.path.getmtime(resolved_file_path)
            except:
                logger.warning('Unable to get the file modification time for the existing lookup file="%s"', resolved_file_path)
                file_time = None
            
            # If we couldn't get the time, then just use the current time (the time we are making a backup)
            if file_time is None:
                file_time = time.time()
            
            # Make the full paths for the backup to be stored
            dst = make_splunkhome_path([backup_directory, str(file_time)])
    
            # Make the backup
            shutil.copyfile(resolved_file_path, dst)
            
            # Copy the permissions and timestamps
            shutil.copystat(resolved_file_path, dst)
            
            logger.info('A backup of the lookup file was created, namespace=%s, lookup_file="%s", backup_file="%s"', namespace, lookup_file, dst)
            
            # Return the path of the backup in case the caller wants to do something with it
            return dst
        except:
            logger.exception("Error when attempting to make a backup; the backup will not be made")
            
            return None
        
    @expose_page(must_login=True, methods=['POST']) 
    def save(self, lookup_file, contents, namespace, **kwargs):
        """
        Save the contents of a lookup file
        """

        logger.info("Saving lookup contents...")

        user = cherrypy.session['user']['name']
        session_key = cherrypy.session.get('sessionKey')
        owner = kwargs.get("owner", "nobody")
        
        # Check capabilities
        LookupEditor.check_capabilities(lookup_file, user, session_key)
        
        # Ensure that the file name is valid
        if not self.is_file_name_valid(lookup_file):
            cherrypy.response.status = 400
            return self.render_error_json(_("The lookup filename contains disallowed characters"))
        
        # Determine the final path of the file
        resolved_file_path = self.resolve_lookup_filename(lookup_file, namespace, owner, throw_not_found=False)
        
        # Make a backup
        self.backupLookupFile(lookup_file, namespace, owner)
        
        # Parse the JSON
        parsed_contents = json.loads(contents)
        
        # Create the temporary file
        temp_file_handle = lookupfiles.get_temporary_lookup_file()
        
        # This is a full path already; no need to call make_splunkhome_path().
        temp_file_name = temp_file_handle.name
        
        # Note that this path will only be used if the lookup file doesn't already exist
        destination_full_path = make_splunkhome_path(['etc', 'apps', namespace, 'lookups', lookup_file])
        
        # Make the lookups directory if it does not exist
        destination_lookups_path = make_splunkhome_path(['etc', 'apps', namespace, 'lookups'])
        
        try:
            os.umask(0) # http://bytes.com/topic/python/answers/572176-os-mkdir-mode
            os.mkdir(destination_lookups_path, 0755)
        except OSError:
            # The directory already existed, no need to create it
            pass
        
        # Write out the new file to a temporary location
        try:
            if temp_file_handle is not None and os.path.isfile(temp_file_name):
                
                csv_writer = csv.writer(temp_file_handle, lineterminator='\n')
                
                for row in parsed_contents:
                    
                    if not isEmpty(row): # Prune empty rows
                        csv_writer.writerow( row )
        
        finally:
            if temp_file_handle is not None:
                temp_file_handle.close()
        
        # Determine if the lookup file exists, create it if it doesn't
        if resolved_file_path is None: #and not os.path.exists(destination_full_path):
            shutil.move(temp_file_name, destination_full_path)
            logger.info('Lookup created successfully, user=%s, namespace=%s, lookup_file=%s', user, namespace, lookup_file)
            
            # If the file is new, then make sure that the list is reloaded so that the editors notice the change
            lookupfiles.SplunkLookupTableFile.reload(session_key=session_key)
            
        # Edit the existing lookup otherwise
        else:
            
            if "owner" in kwargs:
                owner = kwargs["owner"]
            else:
                owner = "nobody"
            
            try:
                
                if not self.isLookupInUsersPath(resolved_file_path) or owner == 'nobody':
                    lookupfiles.update_lookup_table(filename=temp_file_name, lookup_file=lookup_file, namespace=namespace, owner="nobody", key=session_key)
                else:
                    lookupfiles.update_lookup_table(filename=temp_file_name, lookup_file=lookup_file, namespace=namespace, owner=owner, key=session_key)
                
            except AuthorizationFailed as e:
                cherrypy.response.status = 403
                return self.render_error_json(_(str(e)))
                
            logger.info('Lookup edited successfully, user=%s, namespace=%s, lookup_file=%s', user, namespace, lookup_file)
            
        # Tell the SHC environment to replicate the tile
        force_lookup_replication(namespace, lookup_file, session_key)
     
    def render_error_json(self, msg):
        output = jsonresponse.JsonResponse()
        output.data = []
        output.success = False
        output.addError(msg)
        return self.render_json(output, set_mime='text/plain')
    
    def convert_array_to_csv(self, array):
        
        output = StringIO.StringIO() #io.StringIO()
        
        writer = csv.writer(output)
        
        for row in array:
            writer.writerow(row)
            
        return output.getvalue()
    
    @expose_page(must_login=True, methods=['GET']) 
    def get_original_lookup_file(self, lookup_file, namespace="lookup_editor", **kwargs):
        """
        Provides the contents of a lookup file.
        """
        
        lookup_type = kwargs.get("type", "csv")
        
        logger.info("Exporting lookup, namespace=%s, lookup=%s, type=%s, owner=%s", namespace, lookup_file, lookup_type, None)
        
        try:
            
            # If we are getting the CSV, then just pipe the file to the user
            if lookup_type == "csv":
                with self.get_lookup(lookup_file, namespace, None) as f:
                    csvData = f.read()
                
            # If we are getting a KV store lookup, then convert it to a CSV file
            else:
                rows = self.get_kv_lookup(lookup_file, namespace, None)
                
                csvData = self.convert_array_to_csv(rows)
                
            # Tell the browser to download this as a file
            if lookup_file.endswith(".csv"):
                cherrypy.response.headers['Content-Disposition'] = 'attachment; filename="%s"' % lookup_file
            else:
                cherrypy.response.headers['Content-Disposition'] = 'attachment; filename="%s"' % (lookup_file + ".csv")
                
            cherrypy.response.headers['Content-Type'] = 'text/csv'
            return csvData
            
        except IOError:
            cherrypy.response.status = 404
            return self.render_json([])
        
        except PermissionDeniedException as e:
            cherrypy.response.status = 403
            return self.render_error_json(_(str(e)))
    
    @classmethod
    def check_capabilities(cls, lookup_file, user, session_key ):
        
        return
        
        # Get the user's name and session
        user = cherrypy.session['user']['name'] 
        session_key = cherrypy.session.get('sessionKey')
        
        # Get capabilities
        capabilities = LookupEditor.getCapabilities4User(user, session_key)
        
        # Check capabilities
        """
        if False:
            raise PermissionDeniedException(signature)
        """
    
    def isLookupInUsersPath(self, lookup_file_path):
        if "etc/users/" in lookup_file_path:
            return True
        else:
            return False
    
    def resolve_lookup_filename(self, lookup_file, namespace="lookup_editor", owner=None, get_default_csv=True, version=None, throw_not_found=True):
        """
        Resolve the lookup filename.
        """
        
        # Strip out invalid characters like ".." so that this cannot be used to conduct an directory traversal
        lookup_file = os.path.basename(lookup_file)
        namespace = os.path.basename(namespace)
        
        if owner is not None:
            owner = os.path.basename(owner)
        
        # Determine the lookup path by asking Splunk
        try:
            resolved_lookup_path = lookupfiles.SplunkLookupTableFile.get(lookupfiles.SplunkLookupTableFile.build_id(lookup_file, namespace, owner)).path
        except ResourceNotFound:
            if throw_not_found:
                raise
            else:
                return None
        
        if version is not None and owner is not None:
            lookup_path = make_splunkhome_path([self.getBackupDirectory(lookup_file, namespace, owner, resolved_lookup_path=resolved_lookup_path), version])
            lookup_path_default = make_splunkhome_path(["etc", "users", owner, namespace, "lookups", lookup_file + ".default"])
        elif version is not None:
            lookup_path = make_splunkhome_path([self.getBackupDirectory(lookup_file, namespace, owner, resolved_lookup_path=resolved_lookup_path), version])
            lookup_path_default = make_splunkhome_path(["etc", "apps", namespace, "lookups", lookup_file + ".default"])
        elif owner is not None:
            # e.g. $SPLUNK_HOME/etc/users/luke/SA-NetworkProtection/lookups/test.csv
            lookup_path = resolved_lookup_path
            #lookup_path = make_splunkhome_path(["etc", "users", owner, namespace, "lookups", lookup_file])
            lookup_path_default = make_splunkhome_path(["etc", "users", owner, namespace, "lookups", lookup_file + ".default"])
        else:
            lookup_path = resolved_lookup_path
            #lookup_path = make_splunkhome_path(["etc", "apps", namespace, "lookups", lookup_file])
            lookup_path_default = make_splunkhome_path(["etc", "apps", namespace, "lookups", lookup_file + ".default"])
            
        logger.info('Resolved lookup file, path=%s', lookup_path)
            
        # Get the file path
        if get_default_csv and not os.path.exists(lookup_path) and os.path.exists(lookup_path_default):
            return lookup_path_default
        else:
            return lookup_path
            
    def append_if_not_none(self, prefix, key, separator="."):
        
        if prefix is not None and len(prefix) > 0:
            return prefix + separator + key
        else:
            return key
            
    def flatten_dict(self, dict_source, output=None, prefix=''):
        """
        Flatten a dictionary to an array
        """
        
        # Define the resulting output if it does not exist yet
        if output is None:
            output = {}
        
        # Convert each entry in the dictionary
        for key in dict_source:
            
            value = dict_source[key]
            
            # If the value is a dictionary ...
            if isinstance(value, dict):
                self.flatten_dict(value, output, self.append_if_not_none(prefix, key))
                
            #
            else:
                output[self.append_if_not_none(prefix, key)] = value
                
        return output
            
    def get_kv_lookup(self, lookup_file, namespace="lookup_editor", owner=None):
        """
        Get the contents of a KV store lookup.
        """
        
        try:
            
            if owner is None:
                owner = 'nobody'
            
            # Get the session key
            session_key = cherrypy.session.get('sessionKey')
            lookup_contents = []
            
            # Get the fields so that we can compose the header
            _, content = splunk.rest.simpleRequest('/servicesNS/nobody/' + namespace + '/storage/collections/config/' + lookup_file, sessionKey=session_key, getargs={'output_mode': 'json'})
            header = json.loads(content)
            
            fields = ['_key']
            
            for field in header['entry'][0]['content']:
                if field.startswith('field.'):
                    fields.append(field[6:])
            
            lookup_contents.append(fields)
            
            # Get the contents        
            _, content = splunk.rest.simpleRequest('/servicesNS/' + owner + '/' + namespace + '/storage/collections/data/' + lookup_file, sessionKey=session_key, getargs={'output_mode': 'json'})
                
            rows = json.loads(content)
            
            for row in rows:
                new_row = []
                
                flattened_row = self.flatten_dict(row)
                
                for field in fields:
                    if field in flattened_row:
                        new_row.append(flattened_row[field])
            
                lookup_contents.append(new_row)
                
            return lookup_contents
        
        except:
            logger.exception("KV store lookup could not be loaded")
            
    def get_lookup(self, lookup_file, namespace="lookup_editor", owner=None, get_default_csv=True, version=None, throw_exception_if_too_big=False):
        """
        Get a file handle to the associated lookup file.
        """
        
        logger.debug("Version is:" + str(version))
        
        # Get the user's name and session
        user = cherrypy.session['user']['name'] 
        session_key = cherrypy.session.get('sessionKey')
        
        # Check capabilities
        LookupEditor.check_capabilities(lookup_file, user, session_key)
        
        # Get the file path
        file_path = self.resolve_lookup_filename(lookup_file, namespace, owner, get_default_csv, version)
        
        if throw_exception_if_too_big:
            
            try:
                file_size = os.path.getsize(file_path)
                logger.info('Size of lookup file determined, file_size=%s, path=%s', file_size, file_path)
                if file_size > LookupEditor.MAXIMUM_EDITABLE_SIZE:
                    raise LookupFileTooBigException(file_size)
            
            except os.error:
                logger.exception("Exception generated when attempting to determine size of requested lookup file")
        
        logger.info("Loading lookup file from path=%s", file_path)
        
        # Get the file handle
        return open(file_path, 'rb')

    @expose_page(must_login=True, methods=['GET']) 
    def get_lookup_contents(self, lookup_file, namespace="lookup_editor", owner=None, header_only=False, version=None, lookup_type=None, **kwargs):
        """
        Provides the contents of a lookup file as JSON.
        """
        
        logger.info("Retrieving lookup contents, namespace=%s, lookup=%s, type=%s, owner=%s, version=%s", namespace, lookup_file, lookup_type, owner, version)
                        
        if lookup_type is None or len(lookup_type) == '':
            lookup_type = "csv"
            logger.warning("No type for the lookup provided when attempting to load a lookup file, it will default to CSV")
        
        if header_only in ["1", "true", 1, True]:
            header_only = True
        else:
            header_only = False
        
        try:
            
            # Load the KV store lookup
            if lookup_type == "kv":
                return self.render_json(self.get_kv_lookup(lookup_file, namespace, owner))
                
            # Load the CSV lookup
            elif lookup_type == "csv":
                
                with self.get_lookup(lookup_file, namespace, owner, version=version, throw_exception_if_too_big=True) as csv_file:
                    
                    csv_reader = csv.reader(csv_file)
                
                    # Convert the content to JSON
                    lookup_contents = []
                    
                    for row in csv_reader:
                        lookup_contents.append(row)
                        
                        # If we are only loading the header, then stop here
                        if header_only:
                            break
                    
                    return self.render_json(lookup_contents)
            
        except IOError:
            cherrypy.response.status = 404
            return self.render_json([])
        
        except PermissionDeniedException as e:
            cherrypy.response.status = 403
            return self.render_error_json(_(str(e)))
        
        except LookupFileTooBigException as e:
            cherrypy.response.status = 420
            return self.render_json({
                                     'message': 'Lookup file is too large to load (file-size must be less than 10 MB to be edited)',
                                     'file_size' : e.file_size
                                     })
            
        
    @expose_page(must_login=True, methods=['GET']) 
    def get_lookup_header(self, lookup_file, namespace="lookup_editor", owner=None, **kwargs):
        pass