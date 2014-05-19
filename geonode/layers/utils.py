# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2012 OpenPlans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

"""Utilities for managing GeoNode layers
"""

# Standard Modules
import logging
from zipfile import ZipFile
from random import choice
import re
import os
import glob
import sys

from osgeo import gdal

# Django functionality
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _
from django.template.defaultfilters import slugify
from django.core.files import File
from django.core.files.base import ContentFile
from django.contrib.gis.gdal import DataSource
from django.conf import settings

# Geonode functionality
from geonode import GeoNodeException
from geonode.people.utils import get_valid_user
from geonode.layers.models import Layer, UploadSession, SpatialRepresentationType, TopicCategory
from geonode.base.models import Link
from geonode.layers.models import shp_exts, csv_exts, kml_exts, vec_exts, cov_exts
from geonode.utils import http_client
from geonode.layers.metadata import set_metadata

from urlparse import urljoin

from zipfile import ZipFile
from tempfile import mkstemp
from shutil import move

logger = logging.getLogger('geonode.layers.utils')

_separator = '\n' + ('-' * 100) + '\n'


def _clean_string(str, regex=r"(^[^a-zA-Z\._]+)|([^a-zA-Z\._0-9]+)", replace="_"):
    """
    Replaces a string that matches the regex with the replacement.
    """
    regex = re.compile(regex)

    if str[0].isdigit():
        str = replace + str

    return regex.sub(replace, str)


def get_files(filename):
    """Converts the data to Shapefiles or Geotiffs and returns
       a dictionary with all the required files
    """
    files = {}

    # Verify if the filename is in ascii format.
    try:
        filename.decode('ascii')
    except UnicodeEncodeError:
        msg = "Please use only characters from the english alphabet for the filename. '%s' is not yet supported." % os.path.basename(filename).encode('UTF-8')
        raise GeoNodeException(msg)

    # Make sure the file exists.
    if not os.path.exists(filename):
        msg = ('Could not open %s. Make sure you are using a '
               'valid file' % filename)
        logger.warn(msg)
        raise GeoNodeException(msg)

    base_name, extension = os.path.splitext(filename)
    #Replace special characters in filenames - []{}()
    glob_name = re.sub(r'([\[\]\(\)\{\}])', r'[\g<1>]', base_name)

    required_extensions = dict(
        shp='.[sS][hH][pP]', dbf='.[dD][bB][fF]', shx='.[sS][hH][xX]', prj='.[pP][rR][jJ]')
    if extension.lower() == '.shp':
        for ext, pattern in required_extensions.iteritems():
            matches = glob.glob(glob_name + pattern)
            if len(matches) == 0:
                msg = ((_('Expected helper file does not exist: ') + base_name + "." + ext + 
                _('; a Shapefile requires helper files with the following extensions: ') 
                + '%s')) % (required_extensions.keys())
                raise GeoNodeException(msg)
            elif len(matches) > 1:
                msg = ('Multiple helper files for %s exist; they need to be '
                       'distinct by spelling and not just case.') % filename
                raise GeoNodeException(msg)
            else:
                files[ext] = matches[0]

        matches = glob.glob(glob_name + ".[pP][rR][jJ]")
        if len(matches) == 1:
            files['prj'] = matches[0]
        elif len(matches) > 1:
            msg = ('Multiple helper files for %s exist; they need to be '
                   'distinct by spelling and not just case.') % filename
            raise GeoNodeException(msg)
    elif extension.lower() == '.zip':
        zip = ZipFile(filename)
        zipFiles = zip.namelist()

        for file in zipFiles:
            shapefile, extension = os.path.splitext(file)
            if extension.lower() == '.shp':
                base_name = shapefile
            elif extension.lower() == '.sld':
                sldFile = open(mkstemp()[1], "wb")
                sldFile.write(zip.read(file))
                sldFile.close()
                files['sld'] =  sldFile.name

        zipString = ' '.join(zipFiles)
        logger.debug('zipString:%s', zipString)

        for ext, pattern in required_extensions.iteritems():
            logger.debug('basename + pattern:%s', base_name+pattern)
            if re.search(re.escape(base_name) + pattern, zipString) is None:
                msg = ((_('Expected helper file does not exist: ') + base_name + "." + ext + 
                _('; a Shapefile requires helper files with the following extensions: ') 
                + '%s')) % (required_extensions.keys())
                raise GeoNodeException(msg)

        files['zip'] = filename

    elif extension.lower() in cov_exts:
        files[extension.lower().replace('.','')] = filename

    matches = glob.glob(glob_name + ".[sS][lL][dD]")
    if len(matches) == 1:
        files['sld'] = matches[0]
    elif len(matches) > 1:
        msg = ('Multiple style files for %s exist; they need to be '
               'distinct by spelling and not just case.') % filename
        raise GeoNodeException(msg)

    matches = glob.glob(base_name + ".[xX][mM][lL]")

    # shapefile XML metadata is sometimes named base_name.shp.xml
    # try looking for filename.xml if base_name.xml does not exist
    if len(matches) == 0:
        matches = glob.glob(filename + ".[xX][mM][lL]")

    if len(matches) == 1:
        files['xml'] = matches[0]
    elif len(matches) > 1:
        msg = ('Multiple XML files for %s exist; they need to be '
               'distinct by spelling and not just case.') % filename
        raise GeoNodeException(msg)

    return files


def layer_type(filename):
    """Finds out if a filename is a Feature or a Vector
       returns a gsconfig resource_type string
       that can be either 'featureType' or 'coverage'
    """
    base_name, extension = os.path.splitext(filename)

    if extension.lower() == '.zip':
        zf = ZipFile(filename)
        # ZipFile doesn't support with statement in 2.6, so don't do it
        try:
            for n in zf.namelist():
                b, e = os.path.splitext(n.lower())
                if e in shp_exts or e in cov_exts or e in csv_exts:
                    base_name, extension = b,e
        finally:
            zf.close()

    if extension.lower() in vec_exts:
         return 'vector'
    elif extension.lower() in cov_exts:
         return 'raster'
    else:
        msg = ('Saving of extension [%s] is not implemented' % extension)
        raise GeoNodeException(msg)


def get_valid_name(layer_name):
    """
    Create a brand new name
    """

    name = _clean_string(layer_name)
    proposed_name = name.lower()
    count = 1
    while Layer.objects.filter(name=proposed_name).count() > 0:
        proposed_name = "%s_%d" % (name, count)
        count = count + 1
        logger.info('Requested name already used; adjusting name '
                    '[%s] => [%s]', layer_name, proposed_name)
    else:
        logger.info("Using name as requested")

    return proposed_name


def get_valid_layer_name(layer, overwrite):
    """Checks if the layer is a string and fetches it from the database.
    """
    # The first thing we do is get the layer name string
    if isinstance(layer, Layer):
        layer_name = layer.name
    elif isinstance(layer, basestring):
        layer_name = layer
    else:
        msg = ('You must pass either a filename or a GeoNode layer object')
        raise GeoNodeException(msg)

    # Trim the layer name's length to 40 chars.
    # Workaround for issue #354.
    # https://github.com/GeoNode/geonode/issues/354
    if len(layer_name)>40:
    	layer_name = layer_name[:40]

    if overwrite:
        return layer_name
    else:
        return get_valid_name(layer_name)


def get_default_user():
    """Create a default user
    """
    superusers = User.objects.filter(is_superuser=True).order_by('id')
    if superusers.count() > 0:
        # Return the first created superuser
        return superusers[0]
    else:
        raise GeoNodeException('You must have an admin account configured '
                               'before importing data. '
                               'Try: django-admin.py createsuperuser')


def is_vector(filename):
    __, extension = os.path.splitext(filename)

    if extension in vec_exts:
        return True
    else:
        return False 

def is_raster(filename):
    __, extension = os.path.splitext(filename)

    if extension in cov_exts:
        return True
    else:
        return False 

def get_resolution(filename):
    gtif = gdal.Open(filename)
    gt= gtif.GetGeoTransform()
    __, resx, __, __, __, resy = gt
    resolution = '%s %s' % (resx, resy)
    return resolution

def get_bbox(filename):
    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = None, None, None, None

    if is_vector(filename):
        datasource = DataSource(filename)
        layer = datasource[0]
        bbox_x0, bbox_y0, bbox_x1, bbox_y1 = layer.extent.tuple

    elif is_raster(filename):
        gtif = gdal.Open(filename)
        gt= gtif.GetGeoTransform()
        cols = gtif.RasterXSize
        rows = gtif.RasterYSize

        ext=[]
        xarr=[0,cols]
        yarr=[0,rows]

        # Get the extent.
        for px in xarr:
            for py in yarr:
                x=gt[0]+(px*gt[1])+(py*gt[2])
                y=gt[3]+(px*gt[4])+(py*gt[5])
                ext.append([x,y])

            yarr.reverse()

        # ext has four corner points, get a bbox from them.
        bbox_x0 = ext[0][0]
        bbox_y0 = ext[0][1]
        bbox_x1 = ext[2][0]
        bbox_y1 = ext[2][1]

    return [bbox_x0, bbox_x1, bbox_y0, bbox_y1]


def file_upload(filename, name=None, user=None, title=None, abstract=None,
                skip=True, overwrite=False, keywords=[], charset='UTF-8'):
    """Saves a layer in GeoNode asking as little information as possible.
       Only filename is required, user and title are optional.
    """
    # Get a valid user
    theuser = get_valid_user(user)

    # Create a new upload session
    upload_session = UploadSession.objects.create(user=theuser)

    # Get all the files uploaded with the layer
    files = get_files(filename)

    # Add them to the upload session (new file fields are created).
    for type_name, fn in files.items():
        f = open(fn)
        us = upload_session.layerfile_set.create(name=type_name,
                                                file=File(f),
                                                )

    # Set a default title that looks nice ...
    if title is None:
        basename = os.path.splitext(os.path.basename(filename))[0]
        title = basename.title().replace('_', ' ')

    # Create a name from the title if it is not passed.
    if name is None:
        name = slugify(title).replace('-', '_')

    # Generate a name that is not taken if overwrite is False.
    valid_name = get_valid_layer_name(name, overwrite)

    # Get a bounding box
    bbox_x0, bbox_x1, bbox_y0, bbox_y1 = get_bbox(filename)

    defaults = {
                'upload_session': upload_session,
                'title': title,
                'abstract': abstract,
                'owner': user,
                'charset': charset,
                'bbox_x0' : bbox_x0,
                'bbox_x1' : bbox_x1,
                'bbox_y0' : bbox_y0,
                'bbox_y1' : bbox_y1,
    }


    # set metadata
    if 'xml' in files:
        xml_file = open(files['xml'])
        defaults['metadata_uploaded'] = True
        # get model properties from XML
        vals, keywords = set_metadata(xml_file.read())

        for key, value in vals.items():
            if key == 'spatial_representation_type':
                value = SpatialRepresentationType(identifier=value)
            elif key == 'topic_category':
                value, created = TopicCategory.objects.get_or_create(identifier=value.lower(), gn_description=value)
                key = 'category'
            else:
                defaults[key] = value

    # If it is a vector file, create the layer in postgis.
    table_name = None
    if is_vector(filename):
        defaults['storeType'] =  'dataStore'

    # If it is a raster file, get the resolution.
    if is_raster(filename):
        defaults['storeType'] = 'coverageStore'

    # Create a Django object.
    layer, created = Layer.objects.get_or_create(
                         name=valid_name,
                         defaults=defaults
                     )

    # Delete the old layers if overwrite is true
    # and the layer was not just created
    # process the layer again after that by
    # doing a layer.save()
    if not created and overwrite:
        layer.upload_session.layerfile_set.all().delete()
        layer.upload_session = upload_session
        layer.save()

    # Assign the keywords (needs to be done after saving)
    if len(keywords) > 0: 
        layer.keywords.add(*keywords)

    return layer


def upload(incoming, user=None, overwrite=False,
           keywords=(), skip=True, ignore_errors=True,
           verbosity=1, console=None):
    """Upload a directory of spatial data files to GeoNode

       This function also verifies that each layer is in GeoServer.

       Supported extensions are: .shp, .tif, and .zip (of a shapefile).
       It catches GeoNodeExceptions and gives a report per file
    """
    if verbosity > 1:
        print >> console, "Verifying that GeoNode is running ..."

    if console is None:
        console = open(os.devnull, 'w')

    potential_files = []
    if os.path.isfile(incoming):
        ___, short_filename = os.path.split(incoming)
        basename, extension = os.path.splitext(short_filename)
        filename = incoming

        if extension in ['.tif', '.shp', '.zip']:
            potential_files.append((basename, filename))

    elif not os.path.isdir(incoming):
        msg = ('Please pass a filename or a directory name as the "incoming" '
               'parameter, instead of %s: %s' % (incoming, type(incoming)))
        logger.exception(msg)
        raise GeoNodeException(msg)
    else:
        datadir = incoming
        for root, dirs, files in os.walk(datadir):
            for short_filename in files:
                basename, extension = os.path.splitext(short_filename)
                filename = os.path.join(root, short_filename)
                if extension in ['.tif', '.shp', '.zip']:
                    potential_files.append((basename, filename))

    # After gathering the list of potential files,
    # let's process them one by one.
    number = len(potential_files)
    if verbosity > 1:
        msg = "Found %d potential layers." % number
        print >> console, msg

    output = []
    for i, file_pair in enumerate(potential_files):
        basename, filename = file_pair

        existing_layers = Layer.objects.filter(name=basename)

        if existing_layers.count() > 0:
            existed = True
        else:
            existed = False

        if existed and skip:
            save_it = False
            status = 'skipped'
            layer = existing_layers[0]
            if verbosity > 0:
                msg = ('Stopping process because '
                       '--overwrite was not set '
                       'and a layer with this name already exists.')
                print >> sys.stderr, msg
        else:
            save_it = True

        if save_it:
            try:
                layer = file_upload(filename,
                                    user=user,
                                    overwrite=overwrite,
                                    keywords=keywords,
                                )
                if not existed:
                    status = 'created'
                else:
                    status = 'updated'

            except Exception, e:
                if ignore_errors:
                    status = 'failed'
                    exception_type, error, traceback = sys.exc_info()
                else:
                    if verbosity > 0:
                        msg = ('Stopping process because '
                               '--ignore-errors was not set '
                               'and an error was found.')
                        print >> sys.stderr, msg
                        msg = 'Failed to process %s' % filename
                        raise Exception(msg, e), None, sys.exc_info()[2]

        msg = "[%s] Layer for '%s' (%d/%d)" % (status, filename, i + 1, number)
        info = {'file': filename, 'status': status}
        if status == 'failed':
            info['traceback'] = traceback
            info['exception_type'] = exception_type
            info['error'] = error
        else:
            info['name'] = layer.name

        output.append(info)
        if verbosity > 0:
            print >> console, msg
    return output


def _create_featurestore(name, data, overwrite=False, charset="UTF-8"):
    cat = Layer.objects.gs_catalog
    cat.create_featurestore(name, data, overwrite=overwrite, charset=charset)
    return cat.get_store(name), cat.get_resource(name)


def _create_coveragestore(name, data, overwrite=False, charset="UTF-8"):
    cat = Layer.objects.gs_catalog
    cat.create_coveragestore(name, data, overwrite=overwrite)
    return cat.get_store(name), cat.get_resource(name)


def _create_db_featurestore(name, data, overwrite=False, charset="UTF-8"):
    """Create a database store then use it to import a shapefile.

    If the import into the database fails then delete the store
    (and delete the PostGIS table for it).
    """
    cat = Layer.objects.gs_catalog
    dsname = ogc_server_settings.DATASTORE

    try:
        ds = cat.get_store(dsname)
    except FailedRequestError:
        ds = cat.create_datastore(dsname)
        db = ogc_server_settings.datastore_db
        db_engine = 'postgis' if \
            'postgis' in db['ENGINE'] else db['ENGINE']
        ds.connection_parameters.update(
            host = db['HOST'],
            port = db['PORT'],
            database = db['NAME'],
            user = db['USER'],
            passwd = db['PASSWORD'],
            dbtype = db_engine
            )
        cat.save(ds)
        ds = cat.get_store(dsname)

    try:
        cat.add_data_to_store(ds, name, data,
                              overwrite=overwrite,
                              charset=charset)
        return ds, cat.get_resource(name, store=ds)
    except:
        store_params = ds.connection_parameters
        if store_params['dbtype'] and store_params['dbtype'] == 'postgis':
            delete_from_postgis(name)
        else:
            cat.delete(ds, purge=True)
        raise

def style_update(request, url):
    """
    Sync style stuff from GS to GN.
    Ideally we should call this from a view straight from GXP, and we should use
    gsConfig, that at this time does not support styles updates. Before gsConfig
    is updated, for now we need to parse xml.
    In case of a DELETE, we need to query request.path to get the style name,
    and then remove it.
    In case of a POST or PUT, we need to parse the xml from
    request.raw_post_data, which is in this format:
    """
    if request.method in ('POST', 'PUT'): # we need to parse xml
        import xml.etree.ElementTree as ET
        tree = ET.ElementTree(ET.fromstring(request.raw_post_data))
        elm_namedlayer_name=tree.findall('.//{http://www.opengis.net/sld}Name')[0]
        elm_user_style_name=tree.findall('.//{http://www.opengis.net/sld}Name')[1]
        elm_user_style_title=tree.find('.//{http://www.opengis.net/sld}Title')
        if not elm_user_style_title:
            elm_user_style_title = elm_user_style_name
        layer_name=elm_namedlayer_name.text
        style_name=elm_user_style_name.text
        sld_body='<?xml version="1.0" encoding="UTF-8"?>%s' % request.raw_post_data
        if request.method == 'POST': # add style in GN and associate it to layer
            style = Style(name=style_name, sld_body=sld_body, sld_url=url)
            style.save()
            layer = Layer.objects.all().filter(typename=layer_name)[0]
            style.layer_styles.add(layer)
            style.save()
        if request.method == 'PUT': # update style in GN
            style = Style.objects.all().filter(name=style_name)[0]
            style.sld_body=sld_body
            style.sld_url=url
            if len(elm_user_style_title.text)>0:
                style.sld_title = elm_user_style_title.text
            style.save()
            for layer in style.layer_styles.all():
                layer.update_thumbnail()
    if request.method == 'DELETE': # delete style from GN
        style_name = os.path.basename(request.path)
        style = Style.objects.all().filter(name=style_name)[0]
        style.delete()

def create_thumbnail(instance, thumbnail_remote_url):
    BBOX_DIFFERENCE_THRESHOLD = 1e-5

    #Check if the bbox is invalid
    valid_x = (float(instance.bbox_x0) - float(instance.bbox_x1))**2 > BBOX_DIFFERENCE_THRESHOLD
    valid_y = (float(instance.bbox_y1) - float(instance.bbox_y0))**2 > BBOX_DIFFERENCE_THRESHOLD

    image = None

    if valid_x and valid_y:
        Link.objects.get_or_create(resource= instance.resourcebase_ptr,
                        url=thumbnail_remote_url,
                        defaults=dict(
                            extension='png',
                            name=_("Remote Thumbnail"),
                            mime='image/png',
                            link_type='image',
                            )
                        )

        # Download thumbnail and save it locally.
        resp, image = http_client.request(thumbnail_remote_url)

        if 'ServiceException' in image or resp.status < 200 or resp.status > 299:
            msg = 'Unable to obtain thumbnail: %s' % image
            logger.debug(msg)
            # Replace error message with None.
            image = None

    if image is not None:
        if instance.has_thumbnail():
            instance.thumbnail.thumb_file.delete()

        instance.thumbnail.thumb_file.save('layer-%s-thumb.png' % instance.id, ContentFile(image))
        instance.thumbnail.thumb_spec = thumbnail_remote_url
        instance.thumbnail.save()

        thumbnail_url = urljoin(settings.SITEURL, instance.thumbnail.thumb_file.url)

        Link.objects.get_or_create(resource= instance.resourcebase_ptr,
                        url=thumbnail_url,
                        defaults=dict(
                            name=_('Thumbnail'),
                            extension='png',
                            mime='image/png',
                            link_type='image',
                            )
                        )
