import select
import errno
import time
import hashlib
import re
from datetime import datetime

from django.core.exceptions import ValidationError, PermissionDenied
from django.db import models, connection
from django.utils.timezone import utc

from Deb import Deb
from Rpm import Rpm
from Resource import Resource

DISTRO_CHOICES = ( ( 'debian', 'Debian' ), ( 'centos', 'Centos' ), ( 'rhel', 'RHEL' ), ( 'sles', 'SLES' ), ( 'core', 'CoreOS' ), ( 'none', 'None' ) ) # there is no ubuntu, it shares the same version space as debian
MANAGER_TYPE_CHOICES = ( ( 'apt', 'APT' ), ( 'yum', 'YUM' ), ( 'yast', 'YaST' ), ( 'json', 'JSON' ) )
FILE_TYPE_CHOICES = ( ( 'deb', 'deb' ), ( 'rpm', 'RPM' ), ( 'rsc', 'Resource' ) )
FILE_ARCH_CHOICES = ( ( 'x86_64', 'x86_64' ), ( 'i386', 'i386' ), ( 'all', 'All' ) )

# if these are changed (or any other field length), make sure to update the sqlite db in packrat-agent
MANAGER_TYPE_LENGTH = 6
FILE_TYPE_LENGTH = 3
FILE_ARCH_LENGTH = 6
DISTRO_LENGTH = 6

class ReleaseType( models.Model ):
  name = models.CharField( max_length=10, primary_key=True )
  description = models.CharField( max_length=100 )
  level = models.IntegerField() # can promote to a higher level, highest level on a package file is the promotion level
  change_control_required = models.BooleanField( default=False )
  created = models.DateTimeField( editable=False, auto_now_add=True )
  updated = models.DateTimeField( editable=False, auto_now=True )

  def save( self, *args, **kwargs ):
    if self.level < 1 or self.level > 100:
      raise ValidationError( 'Level must be from 1 to 100 inclusive.' )

    if not re.match( '^[0-9a-zA-Z\-_]+$', self.name ):  # possible to be using in a filesystem, must be filesystem safe
      raise ValidationError( 'Invalid ReleaseType Name' )

    super( ReleaseType, self ).save( *args, **kwargs )

  def __unicode__( self ):
    return 'ReleaseType "%s"(%s)' % ( self.description, self.name )

  class API:
    not_allowed_methods = ( 'CREATE', 'DELETE', 'UPDATE' )

class DistroVersion( models.Model ):
  """
This is a type of Distro, ie Centos 6 or Ubuntu 14.04(Trusty)
  """
  # TODO: make the release_names another model
  DISTROS = DISTRO_CHOICES
  FILE_TYPES = FILE_TYPE_CHOICES
  name = models.CharField( max_length=20, primary_key=True )
  distro = models.CharField( max_length=DISTRO_LENGTH, choices=DISTROS ) # TODO: convert into another model
  version = models.CharField( max_length=10 )
  file_type = models.CharField( max_length=FILE_TYPE_LENGTH, choices=FILE_TYPES )
  release_names = models.CharField( max_length=100, blank=True, help_text='tab delimited list of things like el5, trusty, something that is in filename that tells what version it belongs to' )
  created = models.DateTimeField( editable=False, auto_now_add=True )
  updated = models.DateTimeField( editable=False, auto_now=True )

  def save( self, *args, **kwargs ):
    if not re.match( '^[0-9a-zA-Z\-_]+$', self.name ):  # possible to be using in a filesystem, must be filesystem safe
      raise ValidationError( 'Invalid DistroVersion Name' )

    super( DistroVersion, self ).save( *args, **kwargs )

  def __unicode__( self ):
    return 'Version "%s" of "%s"' % ( self.version, self.distro )

  class Meta:
    unique_together = ( 'distro', 'version', 'file_type' )

  class API:
    not_allowed_methods = ( 'CREATE', 'DELETE', 'UPDATE' )
    constants = ( 'DISTROS', 'FILE_TYPES' )


class Repo( models.Model ):
  """
This is a Collection of PackageFiles that meant certian requrements, ie: distro, repo manager, and release type.
  """
  MANAGER_TYPES = MANAGER_TYPE_CHOICES
  name = models.CharField( max_length=50, primary_key=True )
  filesystem_dir = models.CharField( max_length=50 )
  distroversion_list = models.ManyToManyField( DistroVersion )
  manager_type = models.CharField( max_length=MANAGER_TYPE_LENGTH, choices=MANAGER_TYPES )
  description = models.CharField( max_length=200 )
  release_type_list = models.ManyToManyField( ReleaseType )
  created = models.DateTimeField( editable=False, auto_now_add=True )
  updated = models.DateTimeField( editable=False, auto_now=True )

  def poll( self, timeout ):
    cursor = connection.cursor()
    cursor.execute( 'LISTEN "mirror_repo_%s"' % self.pk )
    conn = cursor.cursor.connection
    conn.commit()
    try:
      select.select( [ conn ], [], [], timeout )
    except select.error as e:
      if e[0] == errno.EINTR:
        time.sleep( timeout ) # Self DOS Preventor
      else:
        raise e

    conn.poll()

    result = []
    while conn.notifies:
      notify = conn.notifies.pop() # hopfully notify.channel = the LISTEN name, can't do anything about it if it isn't at this point
      if notify.payload: # is '' if there is not a payload
        result.append( notify.payload )

    return result

  def notify( self, package=None ):
    if package is None:
      connection.cursor().execute( 'NOTIFY "mirror_repo_%s"' % self.pk )
    else:
      connection.cursor().execute( 'NOTIFY "mirror_repo_%s", \'%s\'' % ( self.pk, package.pk ) )

  def save( self, *args, **kwargs ):
    if not re.match( '^[0-9a-zA-Z\-_]+$', self.name ):  # possible to be using in a filesystem, must be filesystem safe
      raise ValidationError( 'Invalid Repo Name' )

    super( Repo, self ).save( *args, **kwargs )

  def __unicode__( self ):
    return 'Repo "%s"' % self.description

  class API:
    not_allowed_methods = ( 'CREATE', 'DELETE', 'UPDATE' )
    constants = ( 'MANAGER_TYPES', )
    actions = {
                'poll': ( { 'type': 'StringList' }, ( { 'type': 'Integer' }, ) )
              }


class Mirror( models.Model ):
  """
This is will authorize a remote server to get a listing of package files.  That list is generated via the repo_list.
NOTE: this dosen't prevent the remote server from downloading an indivvidual file if it allready knows the url, this just controlls the list of files sent.
  """
  name = models.CharField( max_length=50, primary_key=True )
  description = models.CharField( max_length=200 )
  psk = models.CharField( max_length=100 )
  repo_list = models.ManyToManyField( Repo )
  last_heartbeat = models.DateTimeField( editable=False, blank=True, null=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )
  updated = models.DateTimeField( editable=False, auto_now=True )

  def save( self, *args, **kwargs ):
    if not re.match( '^[0-9a-zA-Z\-_]+$', self.name ):  # possible to be using in a filesystem, must be filesystem safe
      raise ValidationError( 'Invalid Mirror Name' )

    super( Mirror, self ).save( *args, **kwargs )

  def __unicode__( self ):
    return 'Mirror "%s"' % self.description

  def heartbeat( self, user ): #TODO: make sure it's the right user for the Mirror
    self.last_heartbeat = datetime.utcnow().replace( tzinfo=utc )
    self.save()

  class API:
    not_allowed_methods = ( 'CREATE', 'DELETE', 'UPDATE' )
    actions = {
                'heartbeat': ( None, ( { 'type': '_USER_' }, ) )
              }


class Package( models.Model ):
  """
This is a Collection of PacageFiles, they share a name.
  """
  name = models.CharField( max_length=200, primary_key=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )
  updated = models.DateTimeField( editable=False, auto_now=True )

  def save( self, *args, **kwargs ):
    if not re.match( '^[0-9a-zA-Z\-]+$', self.name ):  # possible to be using in a filesystem, must be filesystem safe, also don't allow chars that are used to delimit version and other info
      raise ValidationError( 'Invalid Package Name' )

    super( Package, self ).save( *args, **kwargs )

  def __unicode__( self ):
    return 'Package "%s"' % self.name

  class API:
    not_allowed_methods = ( 'DELETE', 'UPDATE' )

class PackageFile( models.Model ): # TODO: add delete to cleanup the file, django no longer does this for us
  """
This is the Individual package "file", they can indivdually belong to any type, arch, package, this is the thing that is actually sent to the remote repos
  """
  FILE_TYPES = FILE_TYPE_CHOICES
  FILE_ARCHS = FILE_ARCH_CHOICES
  package = models.ForeignKey( Package, editable=False, on_delete=models.CASCADE )
  distroversion = models.ForeignKey( DistroVersion, editable=False, on_delete=models.CASCADE )
  version = models.CharField( max_length=50, editable=False )
  type = models.CharField( max_length=FILE_TYPE_LENGTH, editable=False, choices=FILE_TYPES )
  arch = models.CharField( max_length=FILE_ARCH_LENGTH, editable=False, choices=FILE_ARCHS )
  justification = models.TextField()
  provenance = models.TextField()
  file = models.FileField( editable=False )
  sha256 = models.CharField( max_length=64, editable=False )
  release_type = models.ManyToManyField( ReleaseType, through='PackageFileReleaseType' )
  created = models.DateTimeField( editable=False, auto_now_add=True )
  updated = models.DateTimeField( editable=False, auto_now=True )

  @property
  def release( self ):
    try:
      return self.release_type.order_by( '-level' )[0]
    except IndexError:
      return None

  def notify( self, previous_release ):
    repo_list = Repo.objects.filter( release_type_list__in=( previous_release, self.release ), distroversion_list=self.distroversion )

    for repo in repo_list:
      repo.notify( self.package )

  def loadfile( self, file, request_distro ):
    file.file.seek( 0 ) # some upstream process might of left the cursor at the end of the file

    pkgFile = None
    for loader in ( Deb, Rpm, Resource ):
      pkgFile = loader.load( file )
      if pkgFile is not None:
        break

    if not pkgFile:
      raise ValidationError( 'Unable to Determine File Type' )

    try:
      package = Package.objects.get( pk=pkgFile.package )
    except Package.DoesNotExist:
      raise ValidationError( 'Unable to find package "%s"' % pkgFile.package )

    distroversion = None
    distroversion_list = []
    full_distroversion_list = []
    for tmp in DistroVersion.objects.filter( file_type=pkgFile.type ):
      full_distroversion_list.append( tmp.pk )
      for name in tmp.release_names.split( '\t' ):
        if name in pkgFile.version:
          distroversion_list.append( tmp.pk )

    if request_distro:
      if request_distro in full_distroversion_list:
        distroversion = request_distro

    elif len( distroversion_list ) == 1:
      distroversion = distroversion_list[0]

    elif len( full_distroversion_list ) == 1:
      distroversion = full_distroversion_list[0]

    if not distroversion:  # confused, punt to the caller
      if distroversion_list:
        return distroversion_list
      else:
        return full_distroversion_list

    file.file.seek( 0 )
    sha256 = hashlib.sha256()
    while True:
      buf = file.file.read( 4096 )
      if not buf:
        break
      sha256.update( buf )

    # we found one and only one disto, we are taking it
    self.file = file
    self.distroversion_id = distroversion
    self.package = package
    self.type = pkgFile.type
    self.arch = pkgFile.arch
    self.version = pkgFile.version
    self.sha256 = sha256.hexdigest()
    return True

  def promote( self, user, to, change_control_id=None ):
    """
Promote package file to the next release level
    """
    if not user.has_perm( 'Repos.promote_packagefile' ):
      raise PermissionDenied()

    cur_release = None
    try:
      cur_release = self.release_type.order_by( '-level' )[0]
    except IndexError:
      pass

    if cur_release is not None and ( cur_release.level == to.level or cur_release.level >= to.level ):
      raise Exception( 'Unable to promote from "%s"(%s) to "%s"(%s)' % ( cur_release.description, cur_release.name, to.description, to.name ) )

    if to.change_control_required and change_control_id is None:
      raise Exception( 'Change Control required to promote to "%s"(%s)' % ( to.description, to.name ) )

    pfrt = PackageFileReleaseType()
    pfrt.package_file = self
    pfrt.release_type = to
    pfrt.at = datetime.utcnow().replace( tzinfo=utc )
    pfrt.change_control_id = change_control_id
    pfrt.save()

    self.notify( cur_release )

  def deprocate( self, user ):
    """
Deprocate package file.
    """
    if not user.has_perm( 'Repos.promote_packagefile' ):
      raise PermissionDenied()

    previous_release = self.release

    pfrt = PackageFileReleaseType()
    pfrt.package_file = self
    pfrt.release_type = ReleaseType.objects.get( name='depr' )
    pfrt.at = datetime.utcnow().replace( tzinfo=utc )
    pfrt.save()

    self.notify( previous_release )

  @staticmethod
  def create( user, file, justification, provenance, version=None ):
    """
Create a new PackageFile, note version is the distro version and is only required if it
can't be automatically detected, in which case the return value of created will be a list of
possible versions
Return value of None means success
    """
    if not user.has_perm( 'Repos.create_packagefile' ):
      raise PermissionDenied()

    if not version or not version.strip():
      version = None

    try:
      PackageFile.objects.get( file='./%s' % file.name ) #TODO: Figure out where the ./ is comming from and get rid of it, make sure to update the clean up script
      raise Exception( 'File name "%s" allready used' % file.name )
    except PackageFile.DoesNotExist:
      pass

    result = PackageFile()
    result.justification = justification
    result.provenance = provenance
    options = result.loadfile( file, version )

    if options is True:
      result.save()
      pfrt = PackageFileReleaseType()
      pfrt.package_file = result
      pfrt.release_type = ReleaseType.objects.get( name='new' )
      pfrt.at = datetime.utcnow().replace( tzinfo=utc )
      pfrt.save()
      return None

    else:
      return options

  @staticmethod
  def filenameInUse( file_name ):
    try:
      PackageFile.objects.get( file='./%s' % file_name ) #TODO: see ./ comment in create
      return True
    except PackageFile.DoesNotExist:
      pass

    return False

  def save( self, *args, **kwargs ):
    if self.pk and self.file._file:
      raise ValidationError( 'Not Allowed to update the file.' )

    super( PackageFile, self ).save( *args, **kwargs )

  def __unicode__( self ):
    return 'PackageFile "%s"' % ( self.file.name )

  class Meta:
    unique_together = ( 'package', 'distroversion', 'version', 'type', 'arch' )

    default_permissions = ( 'change', 'promote', 'create' )

  class API:
    not_allowed_methods = ( 'CREATE', 'DELETE' )
    constants = ( 'FILE_TYPES', 'FILE_ARCHS' )
    actions = {
               'promote': ( None, ( { 'type': '_USER_' }, { 'type': 'Model', 'model': ReleaseType }, { 'type': 'String' } ) ),
               'deprocate': ( None, ( { 'type': '_USER_' }, ) ),
               'create': ( { 'type': 'StringList' }, ( { 'type': '_USER_' }, { 'type': 'File' }, { 'type': 'String' }, { 'type': 'String' }, { 'type': 'String' } ) ),
               'filenameInUse': ( { 'type': 'Boolean' }, ( { 'type': 'String' }, ) )
              }
    properties = {
                   'release': { 'type': 'Model', 'model': ReleaseType }
                  }
    list_filters = {
                      'package': { 'package': Package },
                      'repo': { 'repo': Repo, 'package_list': 'StringList' }
                   }

    @staticmethod
    def buildQS( qs, user, filter, values ):
      if filter == 'package':
        return qs.filter( package=values[ 'package' ] )

      if filter == 'repo':
        # NOTE: the release type filter is not 100% right, it will work find for most cases, but there are times when this dosen't work, ie a repo that omits a middle level, but that should not happen very often, and until someone comes up with a clever way to fix it, we will have to be happy with this for now
        #   instead of filtering for just the packagefiles with the last releasttype in the list, we are taking the next highest releasetype and removing anything from there up.
        queryset_parms = {}
        queryset_parms[ 'distroversion__in' ] = [ i.pk for i in values[ 'repo' ].distroversion_list.all() ]
        queryset_parms[ 'release_type__in' ] = [ i.pk for i in values[ 'repo' ].release_type_list.all() ]

        if values[ 'package_list' ]: # not None, and not and empty string or empty list
          queryset_parms[ 'package_id__in' ] = values[ 'package_list' ]

        highest_level = max( [ i.level for i in values[ 'repo' ].release_type_list.all() ] )

        return qs.filter( **queryset_parms ).exclude( release_type__in=[ i.pk for i in ReleaseType.objects.filter( level__gt=highest_level ) ] ).distinct()

      raise Exception( 'Invalid filter "%s"' % filter )

class PackageFileReleaseType( models.Model ):
  package_file = models.ForeignKey( PackageFile, on_delete=models.CASCADE )
  release_type = models.ForeignKey( ReleaseType, on_delete=models.CASCADE )
  at = models.DateTimeField( editable=False, auto_now_add=True )
  change_control_id = models.CharField( max_length=50, blank=True, null=True )

  def __unicode__( self ):
    return 'PackageFileReleaseType for PackageFile "%s" Release Type "%s" at "%s"' % ( self.package_file, self.release_type, self.at )

  class Meta:
    unique_together = ( ( 'package_file', 'release_type' ), )
