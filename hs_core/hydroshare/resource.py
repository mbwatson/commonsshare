import os
import zipfile
import shutil
import logging
import requests
import json
import datetime
import base64
from uuid import uuid4


from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.files import File
from django.core.files.uploadedfile import UploadedFile
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from rest_framework import status

from hs_core.models import ResourceFile
from hs_core import signals
from hs_core.hydroshare import utils
from hs_access_control.models import ResourceAccess, UserResourcePrivilege, PrivilegeCodes
from hs_labels.models import ResourceLabels
from hs_core.tasks import notify_fts_indexer
from hs_core.hydroshare import hs_bagit
from django_irods.icommands import SessionException

from minid_client import minid_client_api as mca

METADATA_STATUS_SUFFICIENT = 'Sufficient to publish or make public'
METADATA_STATUS_INSUFFICIENT = 'Insufficient to publish or make public'

logger = logging.getLogger(__name__)

class PublishException(Exception):
    pass

def get_resource(pk):
    """
    Retrieve an instance of type Bags associated with the resource identified by **pk**

    Parameters:    pk - Unique CommonsShare identifier for the resource to be retrieved.

    Returns:    An instance of type Bags.

    Raises:
    Exceptions.NotFound - The resource identified by pid does not exist
    """

    return utils.get_resource_by_shortkey(pk).baseresource.bags.first()


def get_science_metadata(pk):
    """
    Describes the resource identified by the pid by returning the associated science metadata
    object (xml+rdf string). If the resource does not exist, Exceptions.NotFound must be raised.

    REST URL:  GET /scimeta/{pid}

    Parameters:    pk  - Unique CommonsShare identifier for the resource whose science metadata is to
    be retrieved.

    Returns:    Science metadata document describing the resource.

    Return Type:    xml+rdf string

    Raises:    Exceptions.NotAuthorized -  The user is not authorized
    Exceptions.NotFound  - The resource identified by pid does not exist
    Exception.ServiceFailure  - The service is unable to process the request
    """
    res = utils.get_resource_by_shortkey(pk)
    return res.metadata.get_xml()


def get_capabilities(pk):
    """
    Describes API services exposed for a resource.  If there are extra capabilites for a particular
    resource type over and above the standard CommonsShare API, then this API call will list these

    REST URL: GET /capabilites/{pid}

    Parameters: Unique CommonsShare identifier for the resource whose capabilites are to be retrieved.

    Return Type: Capabilites

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request
    """
    res = utils.get_resource_by_shortkey(pk)
    return getattr(res, 'extra_capabilities', lambda: None)()


def get_resource_file(pk, filename):
    """
    Called by clients to get an individual file within a CommonsShare resource.

    REST URL:  GET /resource/{pid}/files/{filename}

    Parameters:
    pid - Unique CommonsShare identifier for the resource from which the file will be extracted.
    filename - The data bytes of the file that will be extracted from the resource identified by pid

    Returns: The bytes of the file extracted from the resource

    Return Type:    pid

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified does not exist or the file identified by filename
    does not exist
    Exception.ServiceFailure - The service is unable to process the request
    """
    resource = utils.get_resource_by_shortkey(pk)
    for f in ResourceFile.objects.filter(object_id=resource.id):
        if f.reference_file_path:
            if f.reference_file_path[1:] == filename:
                return f
        elif os.path.basename(f.resource_file.name) == filename:
            return f.resource_file
    raise ObjectDoesNotExist(filename)


def update_resource_file(pk, filename, f):
    """
    Called by clients to update an individual file within a CommonsShare resource.

    REST URL:  PUT /resource/{pid}/files/{filename}

    Parameters:
    pid - Unique CommonsShare identifier for the resource from which the file will be extracted.
    filename - The data bytes of the file that will be extracted from the resource identified by pid
    file - the data bytes of the file to update

    Returns: The bytes of the file extracted from the resource

    Return Type:    pid

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified does not exist or the file identified by filename
    does not exist
    Exception.ServiceFailure - The service is unable to process the request
    """
    # TODO: does not update metadata; does not check resource state
    resource = utils.get_resource_by_shortkey(pk)
    for rf in ResourceFile.objects.filter(object_id=resource.id):
        if rf.short_path == filename:
            if rf.resource_file:
                # TODO: should use delete_resource_file
                rf.resource_file.delete()
                # TODO: should use add_file_to_resource
                rf.resource_file = File(f) if not isinstance(f, UploadedFile) else f
                rf.save()
            return rf
    raise ObjectDoesNotExist(filename)


def get_related(pk):
    """
    Returns a list of pids for resources that are related to the resource identified by the
    specified pid.

    REST URL:  GET /related/{pid}

    Parameters:
    pid - Unique CommonsShare identifier for the resource whose related resources are to be retrieved.

    Returns:    List of pids for resources that are related to the specified resource.

    Return Type:    List of pids

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request


    """
    raise NotImplemented()


def get_checksum(pk):
    """
    Returns a checksum for the specified resource using the MD5 algorithm. The result is used to
    determine if two instances referenced by a pid are identical.

    REST URL:  GET /checksum/{pid}

    Parameters:
    pid - Unique CommonsShare identifier for the resource for which the checksum is to be returned.

    Returns:    Checksum of the resource identified by pid.

    Return Type:    Checksum

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource specified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request
    """
    raise NotImplementedError()


def check_resource_files(files=()):
    """
    internally used method to check whether the uploaded files are within
    the supported maximal size limit. Also returns sum size of all files for
    quota check purpose if all files are within allowed size limit

    Parameters:
    files - list of Django File or UploadedFile objects to be attached to the resource
    Returns: (status, sum_size) tuple where status is True if files are within FILE_SIZE_LIMIT
             and False if not, and sum_size is the size summation over all files if status is
             True, and -1 if status is False
    """
    sum = 0
    for file in files:
        if not isinstance(file, UploadedFile):
            # if file is already on the server, e.g., a file transferred directly from iRODS,
            # the file should not be subject to file size check since the file size check is
            # only prompted by file upload limit
            if hasattr(file, '_size'):
                sum += int(file._size)
            elif hasattr(file, 'size'):
                sum += int(file.size)
            else:
                try:
                    size = os.stat(file).st_size
                except (TypeError, OSError):
                    size = 0
                sum += size
            continue
        if hasattr(file, '_size') and file._size is not None:
            size = int(file._size)
        elif hasattr(file, 'size') and file.size is not None:
            size = int(file.size)
        else:
            try:
                size = int(os.stat(file.name).st_size)
            except (TypeError, OSError):
                size = 0
        sum += size
    return True, sum


def check_resource_type(resource_type):
    """
    internally used method to check the resource type

    Parameters:
    resource_type: the resource type string to check
    Returns:  the resource type class matching the resource type string; if no match is found,
    returns None
    """
    for tp in utils.get_resource_types():
        if resource_type == tp.__name__:
            res_cls = tp
            break
    else:
        raise NotImplementedError("Type {resource_type} does not exist".format(
            resource_type=resource_type))
    return res_cls


def add_zip_file_contents_to_resource_async(resource, f):
    """
    Launch asynchronous celery task to add zip file contents to a resource.
    Note: will copy the zip file into a temporary space accessible to both
    the Django server and the Celery worker.
    :param resource: Resource to which file should be added
    :param f: TemporaryUploadedFile object (or object that implements temporary_file_path())
     representing a zip file whose contents are to be added to a resource.
    """
    # Add contents of zipfile asynchronously; wait 30 seconds to be "sure" that resource creation
    # has finished.
    uploaded_filepath = f.temporary_file_path()
    tmp_dir = getattr(settings, 'HYDROSHARE_SHARED_TEMP', '/shared_tmp')
    logger.debug("Copying uploaded file from {0} to {1}".format(uploaded_filepath,
                                                                tmp_dir))
    shutil.copy(uploaded_filepath, tmp_dir)
    zfile_name = os.path.join(tmp_dir, os.path.basename(uploaded_filepath))
    logger.debug("Retained upload as {0}".format(zfile_name))
    # Import here to avoid circular reference
    from hs_core.tasks import add_zip_file_contents_to_resource
    add_zip_file_contents_to_resource.apply_async((resource.short_id, zfile_name),
                                                  countdown=30)
    resource.file_unpack_status = 'Pending'
    resource.save()


def create_resource(
        resource_type, owner, title,
        edit_users=None, view_users=None, edit_groups=None, view_groups=None,
        keywords=(), metadata=None, extra_metadata=None,
        files=(), source_names=[], source_sizes=[], move=False, is_file_reference=False,
        create_metadata=True,
        create_bag=True, unpack_file=False, **kwargs):
    """
    Called by a client to add a new resource to CommonsShare. The caller must have authorization to
    write content to CommonsShare. The pid for the resource is assigned by CommonsShare upon inserting
    the resource.  The create method returns the newly-assigned pid.

    REST URL:  POST /resource

    Parameters:

    Returns:    The newly created resource

    Return Type:    BaseResource resource object

    Note:  The calling user will automatically be set as the owner of the created resource.

    Implementation notes:

    1. pid is called short_id.  This is because pid is a UNIX term for Process ID and could be
    confusing.

    2. return type is an instance of hs_core.models.BaseResource class. This is for efficiency in
    the native API.  The native API should return actual instance rather than IDs wherever possible
    to avoid repeated lookups in the database when they are unnecessary.

    3. resource_type is a string: see parameter list

    :param resource_type: string. the type of the resource such as GenericResource
    :param owner: email address, username, or User instance. The owner of the resource
    :param title: string. the title of the resource
    :param edit_users: list of email addresses, usernames, or User instances who will be given edit
    permissions
    :param view_users: list of email addresses, usernames, or User instances who will be given view
    permissions
    :param edit_groups: list of group names or Group instances who will be given edit permissions
    :param view_groups: list of group names or Group instances who will be given view permissions
    :param keywords: string list. list of keywords to add to the resource
    :param metadata: list of dicts containing keys (element names) and corresponding values as
    dicts { 'creator': {'name':'John Smith'}}.
    :param extra_metadata: one dict containing keys and corresponding values
         { 'Outlet Point Latitude': '40', 'Outlet Point Longitude': '-110'}.
    :param files: list of Django File or UploadedFile objects to be attached to the resource
    :param source_names: a list of file names from a federated zone to be
         used to create the resource in the federated zone, default is empty list
    :param source_sizes: a list of file sizes corresponding to source_names if if_file_reference is True; otherwise,
         it is not of any use and should be empty.
    :param move: a value of False or True indicating whether the content files
         should be erased from the source directory. default is False.
    :param is_file_reference: a value of False or True indicating whether the files stored in
        source_files are references to external files without being physically stored in
        HydroShare internally. default is False.
    :param create_bag: whether to create a bag for the newly created resource or not.
        By default, the bag is created.
    :param unpack_file: boolean.  If files contains a single zip file, and unpack_file is True,
        the unpacked contents of the zip file will be added to the resource instead of the zip file.
    :param kwargs: extra arguments to fill in required values in AbstractResource subclasses

    :return: a new resource which is an instance of BaseResource with specificed resource_type.
    """
    if __debug__:
        assert(isinstance(source_names, list))

    with transaction.atomic():
        cls = check_resource_type(resource_type)
        owner = utils.user_from_id(owner)

        # create the resource
        resource = cls.objects.create(
            resource_type=resource_type,
            user=owner,
            creator=owner,
            title=title,
            last_changed_by=owner,
            in_menus=[],
            **kwargs
        )

        resource.resource_type = resource_type

        # by default make resource private
        resource.slug = 'resource{0}{1}'.format('/', resource.short_id)
        resource.save()

        if not metadata:
            metadata = []

        if extra_metadata is not None:
            resource.extra_metadata = extra_metadata
            resource.save()

        if len(files) == 1 and unpack_file and zipfile.is_zipfile(files[0]):
            # Add contents of zipfile as resource files asynchronously
            # Note: this is done asynchronously as unzipping may take
            # a long time (~15 seconds to many minutes).
            add_zip_file_contents_to_resource_async(resource, files[0])
        else:
            # Add resource file(s) now
            # Note: this is done synchronously as it should only take a
            # few seconds.  We may want to add the option to do this
            # asynchronously if the file size is large and would take
            # more than ~15 seconds to complete.

            # made add_resource_files take resource as the first parameter rather than resource id
            # since the extra_data stored on the resource when adding files, e.g., harvested
            # ontology ids, would get lost otherwise
            add_resource_files(resource, *files, source_names=source_names, source_sizes=source_sizes,
                               move=move, is_file_reference=is_file_reference)

        # by default resource is private
        resource_access = ResourceAccess(resource=resource)
        resource_access.save()
        # use the built-in share routine to set initial provenance.
        UserResourcePrivilege.share(resource=resource, grantor=owner, user=owner,
                                    privilege=PrivilegeCodes.OWNER)

        # give read permission to corresponding iRODS user
        if settings.USE_IRODS:
            resource.set_irods_access_control(user_or_group_name=owner.username)

        resource_labels = ResourceLabels(resource=resource)
        resource_labels.save()

        if edit_users:
            for user in edit_users:
                user = utils.user_from_id(user)
                owner.uaccess.share_resource_with_user(resource, user, PrivilegeCodes.CHANGE)

        if view_users:
            for user in view_users:
                user = utils.user_from_id(user)
                owner.uaccess.share_resource_with_user(resource, user, PrivilegeCodes.VIEW)

        if edit_groups:
            for group in edit_groups:
                group = utils.group_from_id(group)
                owner.uaccess.share_resource_with_group(resource, group, PrivilegeCodes.CHANGE)

        if view_groups:
            for group in view_groups:
                group = utils.group_from_id(group)
                owner.uaccess.share_resource_with_group(resource, group, PrivilegeCodes.VIEW)

        if create_metadata:
            # prepare default metadata
            utils.prepare_resource_default_metadata(resource=resource, metadata=metadata,
                                                    res_title=title)

            for element in metadata:
                # here k is the name of the element
                # v is a dict of all element attributes/field names and field values
                k, v = element.items()[0]
                resource.metadata.create_element(k, **v)

            for keyword in keywords:
                resource.metadata.create_element('subject', value=keyword)

            resource.title = resource.metadata.title.value
            resource.save()


    if settings.USE_IRODS:
        # set the resource to private
        resource.setAVU('isPublic', resource.raccess.public)

        # set the resource type (which is immutable)
        resource.setAVU("resourceType", resource._meta.object_name)

        # set quota of this resource to this creator
        resource.set_quota_holder(owner, owner)
    if settings.FTS_URL:
        notify_fts_indexer(resource.short_id)
    return resource


# TODO: this is incredibly misnamed. It should not be used to create empty resources!
def create_empty_resource(pk, user, action='version'):
    """
    Create a resource with empty content and empty metadata for resource versioning or copying.
    This empty resource object is then used to create metadata and content from its original
    resource. This separate routine is needed to return a new resource object to the calling
    view so that if an exception is raised, this empty resource object can be deleted for clean-up.
    Args:
        pk: the unique CommonsShare identifier for the resource that is to be versioned or copied.
        user: the user who requests to create a new version for the resource or copy the resource.
        action: "version" or "copy" with default action being "version"
    Returns:
        the empty new resource that is created as an initial new version or copy for the original
        resource which is then further populated with metadata and content in a subsequent step.
    """
    res = utils.get_resource_by_shortkey(pk)
    if action == 'version':
        if not user.uaccess.owns_resource(res):
            raise PermissionDenied('Only resource owners can create new versions')
    elif action == 'copy':
        # import here to avoid circular import
        from hs_core.views.utils import can_user_copy_resource
        if not user.uaccess.can_view_resource(res):
            raise PermissionDenied('You do not have permission to view this resource')
        allow_copy = can_user_copy_resource(res, user)
        if not allow_copy:
            raise PermissionDenied('The license for this resource does not permit copying')
    else:
        raise ValidationError('Input parameter error: action needs to be version or copy')

    # create the resource without files and without creating bags first
    new_resource = create_resource(
        resource_type=res.resource_type,
        owner=user,
        title=res.metadata.title.value,
        create_metadata=False,
        create_bag=False
    )
    return new_resource


def copy_resource(ori_res, new_res):
    """
    Populate metadata and contents from ori_res object to new_res object to make new_res object
    as a copy of the ori_res object
    Args:
        ori_res: the original resource that is to be copied.
        new_res: the new_res to be populated with metadata and content from the original resource
        as a copy of the original resource.
    Returns:
        the new resource copied from the original resource
    """

    # add files directly via irods backend file operation
    utils.copy_resource_files_and_AVUs(ori_res.short_id, new_res.short_id)

    utils.copy_and_create_metadata(ori_res, new_res)

    hs_identifier = ori_res.metadata.identifiers.all().filter(name="hydroShareIdentifier")[0]
    if hs_identifier:
        new_res.metadata.create_element('source', derived_from=hs_identifier.url)

    if ori_res.resource_type.lower() == "collectionresource":
        # clone contained_res list of original collection and add to new collection
        # note that new collection will not contain "deleted resources"
        new_res.resources = ori_res.resources.all()

    return new_res


def create_new_version_resource(ori_res, new_res, user):
    """
    Populate metadata and contents from ori_res object to new_res object to make new_res object as
    a new version of the ori_res object
    Args:
        ori_res: the original resource that is to be versioned.
        new_res: the new_res to be populated with metadata and content from the original resource
        to make it a new version
        user: the requesting user
    Returns:
        the new versioned resource for the original resource and thus obsolete the original resource

    """
    # newly created new resource version is private initially
    # add files directly via irods backend file operation
    utils.copy_resource_files_and_AVUs(ori_res.short_id, new_res.short_id)

    # copy metadata from source resource to target new-versioned resource except three elements
    utils.copy_and_create_metadata(ori_res, new_res)

    # add or update Relation element to link source and target resources
    hs_identifier = new_res.metadata.identifiers.all().filter(name="hydroShareIdentifier")[0]
    ori_res.metadata.create_element('relation', type='isReplacedBy', value=hs_identifier.url)

    if new_res.metadata.relations.all().filter(type='isVersionOf').exists():
        # the original resource is already a versioned resource, and its isVersionOf relation
        # element is copied over to this new version resource, needs to delete this element so
        # it can be created to link to its original resource correctly
        eid = new_res.metadata.relations.all().filter(type='isVersionOf').first().id
        new_res.metadata.delete_element('relation', eid)

    hs_identifier = ori_res.metadata.identifiers.all().filter(name="hydroShareIdentifier")[0]
    new_res.metadata.create_element('relation', type='isVersionOf', value=hs_identifier.url)

    if ori_res.resource_type.lower() == "collectionresource":
        # clone contained_res list of original collection and add to new collection
        # note that new version collection will not contain "deleted resources"
        new_res.resources = ori_res.resources.all()

    # since an isReplaceBy relation element is added to original resource, needs to call
    # resource_modified() for original resource
    utils.resource_modified(ori_res, user, overwrite_bag=False)
    # if everything goes well up to this point, set original resource to be immutable so that
    # obsoleted resources cannot be modified from REST API
    ori_res.raccess.immutable = True
    ori_res.raccess.save()
    return new_res


def add_resource_files(resource, *files, **kwargs):
    """
    Called by clients to update a resource in CommonsShare by adding one or more files.

    REST URL:  PUT /resource/{pid}/files/{file}

    Parameters:
    pk - Unique CommonsShare identifier for the resource that is to be updated.
    files - A list of file-like objects representing files that will be added
    to the existing resource identified by pid

    Returns:    A list of ResourceFile objects added to the resource

    Return Type:    list

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.InvalidContent - The content of the file is invalid
    Exception.ServiceFailure - The service is unable to process the request

    Notes:
    This does **not** handle mutability; changes to immutable resources should be denied elsewhere.

    """
    ret = []
    source_names = kwargs.pop('source_names', [])
    source_sizes = kwargs.pop('source_sizes', [])
    is_file_reference = kwargs.pop('is_file_reference', False)

    if __debug__:
        assert(isinstance(source_names, list))

    move = kwargs.pop('move', False)
    folder = kwargs.pop('folder', None)

    if __debug__:  # assure that there are no spurious kwargs left.
        for k in kwargs:
            print("kwargs[{}]".format(k))
        assert len(kwargs) == 0

    for f in files:
        file_ext = os.path.splitext(f.name)[1]
        if file_ext.lower() == '.jsonld':
            # treat the file as semantic metadata
            utils.harvest_ontology_ids_from_metadata(resource, f)
            ret.append(utils.add_file_to_resource(resource, f, folder='metadata'))
        else:
            ret.append(utils.add_file_to_resource(resource, f, folder=folder))

    if len(source_names) > 0:
        if len(source_names) != len(source_sizes):
            # if length is not equal, there is an issue with source_sizes input parameter, so it will not be
            # used by setting it to be empty
            source_sizes = []

        idx = 0
        for ifname in source_names:
            s_size = source_sizes[idx] if source_sizes else 0
            idx += 1
            ret.append(utils.add_file_to_resource(resource, None,
                                                  folder=folder,
                                                  source_name=ifname,
                                                  source_size=s_size,
                                                  move=move,
                                                  is_file_reference=is_file_reference))

    # make sure data directory exists if not exist already
    utils.create_empty_contents_directory(resource)

    if settings.FTS_URL:
        notify_fts_indexer(resource.short_id)

    return ret


def update_science_metadata(pk, metadata, user):
    """
    Updates science metadata for a resource

    Args:
        pk: Unique CommonsShare identifier for the resource for which science metadata needs to be
        updated.
        metadata: a list of dictionary items containing data for each metadata element that needs to
        be updated
        user: user who is updating metadata
        example metadata format:
        [
            {'title': {'value': 'Updated Resource Title'}},
            {'description': {'abstract': 'Updated Resource Abstract'}},
            {'date': {'type': 'valid', 'start_date': '1/26/2016', 'end_date': '12/31/2016'}},
            {'creator': {'name': 'John Smith', 'email': 'jsmith@gmail.com'}},
            {'creator': {'name': 'Lisa Molley', 'email': 'lmolley@gmail.com'}},
            {'contributor': {'name': 'Kelvin Marshal', 'email': 'kmarshal@yahoo.com',
                             'organization': 'Utah State University',
                             'profile_links': [{'type': 'yahooProfile', 'url':
                             'http://yahoo.com/LH001'}]}},
            {'coverage': {'type': 'period', 'value': {'name': 'Name for period coverage',
                                                      'start': '1/1/2000',
                                                      'end': '12/12/2012'}}},
            {'coverage': {'type': 'point', 'value': {'name': 'Name for point coverage', 'east':
                                                     '56.45678',
                                                     'north': '12.6789', 'units': 'decimal deg'}}},
            {'identifier': {'name': 'someIdentifier', 'url': "http://some.org/001"}},
            {'language': {'code': 'fre'}},
            {'relation': {'type': 'isPartOf', 'value': 'http://hydroshare.org/resource/001'}},
            {'rights': {'statement': 'This is the rights statement for this resource',
                        'url': 'http://rights.ord/001'}},
            {'source': {'derived_from': 'http://hydroshare.org/resource/0001'}},
            {'subject': {'value': 'sub-1'}},
            {'subject': {'value': 'sub-2'}},
        ]

    Returns:
    """
    resource = utils.get_resource_by_shortkey(pk)
    resource.metadata.update(metadata, user)
    utils.resource_modified(resource, user, overwrite_bag=False)

    # set to private if metadata has become non-compliant
    resource.update_public_and_discoverable()  # set to False if necessary


def delete_resource(pk):
    """
    Deletes a resource managed by CommonsShare. The caller must be an owner of the resource or an
    administrator to perform this function. The operation removes the resource from further
    interaction with CommonsShare services and interfaces. The implementation may delete the resource
    bytes, and should do so since a delete operation may be in response to a problem with the
    resource (e.g., it contains malicious content, is inappropriate, or is subject to a legal
    request). If the resource does not exist, the Exceptions.NotFound exception is raised.

    REST URL:  DELETE /resource/{pid}

    Parameters:
    pid - The unique CommonsShare identifier of the resource to be deleted

    Returns:
    The pid of the resource that was deleted

    Return Type:    pid

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request

    Note:  Only CommonsShare administrators will be able to delete formally published resour
    """

    res = utils.get_resource_by_shortkey(pk)

    if res.metadata.relations.all().filter(type='isReplacedBy').exists():
        raise ValidationError('An obsoleted resource in the middle of the obsolescence chain '
                              'cannot be deleted.')

    # when the most recent version of a resource in an obsolescence chain is deleted, the previous
    # version in the chain needs to be set as the "active" version by deleting "isReplacedBy"
    # relation element
    if res.metadata.relations.all().filter(type='isVersionOf').exists():
        is_version_of_res_link = \
            res.metadata.relations.all().filter(type='isVersionOf').first().value
        idx = is_version_of_res_link.rindex('/')
        if idx == -1:
            obsolete_res_id = is_version_of_res_link
        else:
            obsolete_res_id = is_version_of_res_link[idx+1:]
        obsolete_res = utils.get_resource_by_shortkey(obsolete_res_id)
        if obsolete_res.metadata.relations.all().filter(type='isReplacedBy').exists():
            eid = obsolete_res.metadata.relations.all().filter(type='isReplacedBy').first().id
            obsolete_res.metadata.delete_element('relation', eid)
            # also make this obsoleted resource editable now that it becomes the latest version
            obsolete_res.raccess.immutable = False
            obsolete_res.raccess.save()
    res.delete()
    if settings.FTS_URL:
        notify_fts_indexer(pk)

    return pk


def get_resource_file_name(f):
    """
    get the file name of a specific ResourceFile object f
    Args:
        f: the ResourceFile object to return name for
    Returns:
        the file name of the ResourceFile object f
    """
    return f.storage_path


def delete_resource_file_only(resource, f):
    """
    Delete the single resource file f from the resource without sending signals and
    without deleting related metadata element. This function is called by delete_resource_file()
    function as well as from pre-delete signal handler for specific resource types
    (e.g., netCDF, raster, and feature) where when one resource file is deleted,
    some other resource files needs to be deleted as well.
    Args:
        resource: the resource from which the file f is to be deleted
        f: the ResourceFile object to be deleted
    Returns: unqualified relative path to file that has been deleted
    """
    short_path = f.short_path
    f.delete()
    if settings.FTS_URL:
        notify_fts_indexer(resource.short_id)
    return short_path


def delete_format_metadata_after_delete_file(resource, file_name):
    """
    delete format metadata as appropriate after a file is deleted.
    :param resource: BaseResource object representing a CommonsShare resource
    :param file_name: the file name to be deleted
    :return:
    """
    delete_file_mime_type = utils.get_file_mime_type(file_name)
    delete_file_extension = os.path.splitext(file_name)[1]

    # if there is no other resource file with the same extension as the
    # file just deleted then delete the matching format metadata element for the resource
    resource_file_extensions = [os.path.splitext(get_resource_file_name(f))[1] for f in
                                resource.files.all()]
    if delete_file_extension not in resource_file_extensions:
        format_element = resource.metadata.formats.filter(value=delete_file_mime_type).first()
        if format_element:
            resource.metadata.delete_element(format_element.term, format_element.id)


# TODO: test in-folder delete of short path
def filter_condition(filename_or_id, fl):
    """
    Converted lambda definition of filter_condition into def to conform to pep8 E731 rule: do not
    assign a lambda expression, use a def
    :param filename_or_id: passed in filename_or id as the filter
    :param fl: the ResourceFile object to filter against
    :return: boolean indicating whether fl conforms to filename_or_id
    """
    try:
        file_id = int(filename_or_id)
        return fl.id == file_id
    except ValueError:
        return fl.short_path == filename_or_id


# TODO: Remove option for file id, not needed since names are unique.
# TODO: Test that short_path deletes properly.
def delete_resource_file(pk, filename_or_id, user, delete_logical_file=True):
    """
    Deletes an individual file from a CommonsShare resource. If the file does not exist,
    the Exceptions.NotFound exception is raised.

    REST URL:  DELETE /resource/{pid}/files/{filename}

    Parameters:
    :param pk: The unique CommonsShare identifier for the resource from which the file will be deleted
    :param filename_or_id: Name of the file or id of the file to be deleted from the resource
    :param user: requesting user
    :param delete_logical_file: If True then if the ResourceFile object to be deleted is part of a
    LogicalFile object then the LogicalFile object will be deleted which deletes all associated
    ResourceFile objects and file type metadata objects.

    :returns:    The name or id of the file which was deleted

    Return Type:    string or integer

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist or the file identified by
    file does not exist
    Exception.ServiceFailure - The service is unable to process the request

    Note:  This does not handle immutability as previously intended.
    """
    resource = utils.get_resource_by_shortkey(pk)
    res_cls = resource.__class__

    for f in ResourceFile.objects.filter(object_id=resource.id):
        if filter_condition(filename_or_id, f):
            if delete_logical_file:
                if f.logical_file is not None:
                    # logical_delete() calls this function (delete_resource_file())
                    # to delete each of its contained ResourceFile objects
                    f.logical_file.logical_delete(user)
                    return filename_or_id

            signals.pre_delete_file_from_resource.send(sender=res_cls, file=f,
                                                       resource=resource, user=user)

            # Pabitra: better to use f.delete() here and get rid of the
            # delete_resource_file_only() util function
            file_name = delete_resource_file_only(resource, f)

            # This presumes that the file is no longer in django
            delete_format_metadata_after_delete_file(resource, file_name)

            signals.post_delete_file_from_resource.send(sender=res_cls, resource=resource)

            # set to private if necessary -- AFTER post_delete_file handling
            resource.update_public_and_discoverable()  # set to False if necessary

            # generate bag
            utils.resource_modified(resource, user, overwrite_bag=False)

            return filename_or_id

    # if execution gets here, file was not found
    raise ObjectDoesNotExist(str.format("resource {}, file {} not found",
                                        resource.short_id, filename_or_id))

def publish_resource(user, pk, publish_type):
    """
    Formally publishes a resource in CommonsShare. Triggers the creation of a MINID for the resource,
    and triggers the exposure of the resource to the CommonsShare DataONE Member Node. The user must
    be an owner of a resource or an administrator to perform this action.

    Parameters:
        user - requesting user to publish the resource who must be one of the owners of the resource
        pk - Unique CommonsShare identifier for the resource to be formally published.
        request - request triggering this action
        publish_type - type of identifier minted when the resource is published i.e. DOI or MINID

    Returns:    The id of the resource that was published

    Return Type:    string

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request
    and other general exceptions

    Note:  This is different than just giving public access to a resource via access control rule
    """

    resource_id = pk
    resource = utils.get_resource_by_shortkey(pk)
    res_coll = resource.root_path
    istorage = resource.get_irods_storage()
    resource_url = '{0}/resource/{1}'.format(utils.current_site_url(), resource.short_id)


    # TODO: whether a resource can be published is not considered in can_be_published
    # TODO: can_be_published is currently an alias for can_be_public_or_discoverable
    if not resource.can_be_published:
        raise ValidationError("This resource cannot be published since it does not have required "
                              "metadata or content files or this resource type is not allowed "
                              "for publication.")



    dos_url = '{0}/dosapi/dataobjects/{1}/'.format(utils.current_site_url(), resource.short_id)
    tmpdir = os.path.join(settings.TEMP_FILE_DIR, uuid4().hex)
    resource_file_manifest_json = get_resource_files_manifest(resource)

    os.makedirs(tmpdir)

    # create the remote manifest and metadata files
    resource_manifest_file = os.path.join(tmpdir, 'resource-file-manifest.json')
    with open(resource_manifest_file, 'w') as outfile:
        json.dump(resource_file_manifest_json, outfile)

    if resource.files.all().count() > 1:
        if istorage.exists(res_coll):
            bag_modified = istorage.getAVU(res_coll, 'bag_modified')
        else:
            raise ValidationError("Resource {} does not exist in iRODS".format(resource.short_id))

        if bag_modified is None or bag_modified.lower() == "true":
                hs_bagit.create_bag(resource)

        tmpfile = os.path.join(tmpdir, 'bag.zip')

        bag_full_name = 'bags/{res_id}.zip'.format(res_id=resource_id)
        irods_dest_prefix = settings.IRODS_HOME_COLLECTION
        srcfile = os.path.join(irods_dest_prefix, bag_full_name)
        istorage.getFile(srcfile, tmpfile)
        sha_checksum = mca.compute_checksum(tmpfile)
        size = istorage.size(srcfile)
        download_file_url = '{0}/django_irods/download/bags/{1}.zip'.format(utils.current_site_url(),
                                                                            resource.short_id)
        file_format = 'application/zip'
    else:
        sha_checksum = resource_file_manifest_json[0]['sha256']
        size = resource_file_manifest_json[0]['length']
        file_format = 'text/plain'
        download_file_url = resource_file_manifest_json[0]['url']

    locations = [resource_url, download_file_url]

    if publish_type.lower() == "minid":
        config= mca.parse_config('hydroshare/minid-config.cfg')
        identifier = mca.register_entity(config['minid_server'],
                                    sha_checksum,
                                    config['email'],
                                    config['code'],
                                    locations, 'MINID for ' + resource.title, True)

        resource.minid = identifier
        resource.doi = ''
        ident_md_args = {'name': 'minid',
               'url': 'http://minid.bd2k.org/minid/landingpage/' + resource.minid + ', http://n2t.net/' + resource.minid}
    elif publish_type.lower() == "doi":
        # create DOI using DataCite API
        doi_put_url = settings.DOI_PUT_URL
        request_data = {}
        request_data['@context'] = 'https://schema.org'
        request_data['@type'] = 'Dataset'

        author_data = {}
        author_data['@type'] = 'Organization'
        author_data['@id'] = 'doi:/10.25491/5e92-ht74'
        author_data['name'] = 'Renaissance Computing Institute (RENCI) at the University of North Carolina at Chapel Hill'
        request_data['author'] = [author_data]

        publisher_data = {}
        publisher_data['@type'] = 'Organization'
        publisher_data['name'] = 'CommonsShare'
        publisher_data['url'] = 'www.commonsshare.org'
        request_data['publisher'] = [publisher_data]

        request_data['datePublished'] = repr(datetime.date.today().year)
        request_data['url'] = resource_url
        request_data['name'] = 'DOI for ' + resource.title

        property_value_data = {}
        property_value_data['@type'] = 'PropertyValue'
        property_value_data['propertyID'] = 'sha256'

        request_data['identifier'] = [property_value_data]
        request_data['fileFormat'] = file_format
        request_data['contentSize'] = repr(size)
        request_data['contentUrl'] = [download_file_url, dos_url]

        auth_header_str = "Bearer {}".format(settings.DOI_OAUTH_TOKEN)
        response = requests.put(doi_put_url,
                                data=json.dumps(request_data),
                                headers={"Content-Type": "application/json", "Authorization": auth_header_str })

        if response.status_code != status.HTTP_200_OK:
            logger.error("Error retrieving DOI from datacite service")
            logger.error(response.status_code)
            logger.error(response.text)
            raise PublishException("Unable to retrieve a DOI from DataCite. Resource cannot be published.")
        else:
            logger.info("response content: " + response.content)
            return_data = json.loads(response.content)
            identifier = return_data['@id']
            resource.doi = identifier
            resource.minid = ''
            doi_url = 'https://ors.datacite.org/' + identifier
            ident_md_args = {'name': 'doi',
                   'url': doi_url}

    # remove the temp directory
    shutil.rmtree(tmpdir)

    # register published resource in farishake
    #retrieve an API Key to access FairShake registration API

    request_data = {}
    request_data['username'] = settings.FAIRSHAKE_USERID
    request_data['password'] = settings.FAIRSHAKE_PASSWORD
    response = requests.post(settings.FAIRSHAKE_URL +'/auth/login/',
                            data=json.dumps(request_data),
                            headers={"Content-Type": "application/json"})

    if response.status_code != status.HTTP_200_OK:
        logger.error("Error retrieving APIKey from FairShake API")
        logger.error(response.status_code)
        logger.error(response.text)
        raise PublishException("This resource cannot be published because it has failed the FairShake registration process." + response.text)
    else:
        return_data = json.loads(response.content)
        fairshake_apikey = return_data['key']

    request_data = {}
    request_data["title"] = resource.title
    request_data["tags"] = "dcppc"
    request_data["url"] =  resource_url
    request_data["projects"] = [14]
    request_data["rubrics"] = [11]

    response = requests.post(settings.FAIRSHAKE_URL + '/digital_object/',
                             data=json.dumps(request_data),
                             headers={"accept": "application/json", "Content-Type": "application/json", "Authorization": "Token " + fairshake_apikey})
    if response.status_code != status.HTTP_201_CREATED:
        logger.error("Error registering resource with FairShake")
        logger.error(response.status_code)
        logger.error(response.text)
        raise PublishException(
            "This resource cannot be published because it has failed the FairShake registration process." + response.text)
    else:
        return_data = json.loads(response.content)
        assessment_id = return_data['id']
        logger.info("Created FairShake object with ID: " + repr(assessment_id))
        resource.assessment_id = assessment_id

    resource.save()

    resource.set_public(True)  # also sets discoverable to True
    resource.raccess.immutable = True
    resource.raccess.shareable = False
    resource.raccess.published = True
    resource.raccess.save()

    # change "Publisher" element of science metadata to CommonsShare
    md_args = {'name': 'CommonsShare',
               'url': 'https://www.commonsshare.org'}
    resource.metadata.create_element('Publisher', **md_args)

    # create published date
    resource.metadata.create_element('date', type='published', start_date=resource.updated)

    resource.metadata.create_element('Identifier', **ident_md_args)

    utils.resource_modified(resource, user, overwrite_bag=False)


def resolve_minid(minid):
    """
    Takes as input a MINID and returns the internal CommonsShare identifier (pid) for a resource.
    This method will be used to get the CommonsShare pid for a resource identified by a doi for
    further operations using the web service API.

    REST URL:  GET /resolveMINID/{minid}

    Parameters:    minid - A minid assigned to a resource in CommonsShare.

    Returns:    The minid of the resource that was published

    Return Type:    pid

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request

    Note:  All CommonsShare methods (except this one) will use CommonsShare internal identifiers
    (pids). This method exists so that a program can resolve the pid for a DOI.
    """
    return utils.get_resource_by_minid(minid).short_id


def resolve_doi(doi):
    """
    Takes as input a DOI and returns the internal CommonsShare identifier (pid) for a resource.
    This method will be used to get the CommonsShare pid for a resource identified by a doi for
    further operations using the web service API.

    REST URL:  GET /resolveDOI/{doi}

    Parameters:    doi - A doi assigned to a resource in CommonsShare.

    Returns:    The doi of the resource that was published

    Return Type:    pid

    Raises:
    Exceptions.NotAuthorized - The user is not authorized
    Exceptions.NotFound - The resource identified by pid does not exist
    Exception.ServiceFailure - The service is unable to process the request

    Note:  All CommonsShare methods (except this one) will use CommonsShare internal identifiers
    (pids). This method exists so that a program can resolve the pid for a DOI.
    """
    return utils.get_resource_by_doi(doi).short_id

def create_metadata_element(resource_short_id, element_model_name, **kwargs):
    """
    Creates a specific type of metadata element for a given resource

    :param resource_short_id: id of the resource for which a metadata element needs to be created
    :param element_model_name: metadata element name (e.g., creator)
    :param kwargs: metadata element attribute name/value pairs for all those attributes that
    require a value
    :return:
    """
    res = utils.get_resource_by_shortkey(resource_short_id)
    res.metadata.create_element(element_model_name, **kwargs)


def update_metadata_element(resource_short_id, element_model_name, element_id, **kwargs):
    """
    Updates the data associated with a metadata element for a specified resource

    :param resource_short_id: id of the resource for which a metadata element needs to be updated
    :param element_model_name: metadata element name (e.g., creator)
    :param element_id: id of the metadata element to be updated
    :param kwargs: metadata element attribute name/value pairs for all those attributes that need
    update
    :return:
    """
    res = utils.get_resource_by_shortkey(resource_short_id)
    res.metadata.update_element(element_model_name, element_id, **kwargs)


def delete_metadata_element(resource_short_id, element_model_name, element_id):
    """
    Deletes a specific type of metadata element for a specified resource

    :param resource_short_id: id of the resource for which metadata element to be deleted
    :param element_model_name: metadata element name (e.g., creator)
    :param element_id: id of the metadata element to be deleted
    :return:
    """
    res = utils.get_resource_by_shortkey(resource_short_id)
    res.metadata.delete_element(element_model_name, element_id)

def get_resource_files_manifest(resource):
    data_list = []

    from hs_core.hydroshare import utils

    istorage = resource.get_irods_storage()

    for f in ResourceFile.objects.filter(object_id=resource.id):
        data = {}

        if f.reference_file_path:
            irods_file_name = f.reference_file_path
            srcfile = irods_file_name
            last_sep_pos = irods_file_name.rfind('/')
            ref_file_name = irods_file_name[last_sep_pos + 1:]
            fetch_url = '{0}/django_irods/download/{1}'.format(utils.current_site_url(),
                                                               resource.short_id + irods_file_name)
        else:
            irods_file_name = f.storage_path
            irods_dest_prefix = settings.IRODS_HOME_COLLECTION
            srcfile = os.path.join(irods_dest_prefix, irods_file_name)
            fetch_url = '{0}/django_irods/download/{1}'.format(utils.current_site_url(), irods_file_name)

        checksum = None

        try:
            checksum = istorage.checksum(srcfile)
        except SessionException as ex:
            logger.error(ex.stderr)
        finally:
            data['url'] = fetch_url

            if (f.reference_file_path):
                data['length'] = istorage.size(srcfile)
                data['filename'] = ref_file_name
            else:
                data['length'] = f.size
                data['filename'] = f.file_name

            if checksum is not None:
                if checksum.startswith('sha'):
                    data['sha256'] = base64.b64decode(checksum[4:]).encode('hex')
                elif checksum.startswith('md5'):
                    data['md5'] = base64.b64decode(checksum[4:]).encode('hex')

            data_list.append(data)

    return data_list