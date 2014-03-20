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
import urllib

import uuid
import logging

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.forms.models import modelformset_factory
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.conf import settings
from django.template import RequestContext, loader
from django.utils.translation import ugettext as _
from django.utils import simplejson as json
from django.shortcuts import get_object_or_404


#from geonode.core.layers.views import layer_set_permissions
from geoserver.catalog import Catalog, FailedRequestError
from owslib.wms import WebMapService
from owslib.wfs import WebFeatureService
from owslib.tms import TileMapService
from owslib.csw import CatalogueServiceWeb
from arcrest import Folder as ArcFolder, MapService as ArcMapService
from urlparse import urlsplit, urlunsplit


#from geonode.utils import OGC_Servers_Handler
from geonode.contrib.services.models import Service, Layer, ServiceLayer, WebServiceHarvestLayersJob, WebServiceRegistrationJob
from geonode.maps.views import _perms_info, bbox_to_wkt
from geonode.core.models import AUTHENTICATED_USERS, ANONYMOUS_USERS
from geonode.contrib.services.forms import CreateServiceForm, ServiceLayerFormSet, ServiceForm
from geonode.utils import slugify
import re
from geonode.maps.utils import llbbox_to_mercator, mercator_to_llbbox
from django.db import transaction

logger = logging.getLogger("geonode.core.layers.views")


#ogc_server_settings = OGC_Servers_Handler(settings.OGC_SERVER)['default']

_user, _password = settings.GEOSERVER_CREDENTIALS #ogc_server_settings.credentials

SERVICE_LEV_NAMES = {
    Service.LEVEL_NONE  : _('No Service Permissions'),
    Service.LEVEL_READ  : _('Read Only'),
    Service.LEVEL_WRITE : _('Read/Write'),
    Service.LEVEL_ADMIN : _('Administrative')
}

OGP_ABSTRACT = _("""
The Open Geoportal is a consortium comprised of contributions of several universities and organizations to help
facilitate the discovery and acquisition of geospatial data across many organizations and platforms. Current partners
include: Harvard, MIT, MassGIS, Princeton, Columbia, Stanford, UC Berkeley, UCLA, Yale, and UConn. Built on open source
technology, The Open Geoportal provides organizations the opportunity to share thousands of geospatial data layers,
maps, metadata, and development resources through a single common interface.
""")

@login_required
def services(request):
    """
    This view shows the list of all registered services
    """
    services = Service.objects.all()
    return render_to_response("services/service_list.html", RequestContext(request, {
        'services': services,
    }))

@login_required
def register_service(request):
    """
    This view is used for manually registering a new service, with only URL as a
    parameter.
    """

    if request.method == "GET":
        service_form = CreateServiceForm()
        return render_to_response('services/service_register.html',
                                  RequestContext(request, {
                                      'create_service_form': service_form
                                  }))

    elif request.method == 'POST':
        # Register a new Service
        service_form = CreateServiceForm(request.POST)
        if service_form.is_valid():
            try:
                url = _clean_url(service_form.cleaned_data['url'])

            # method = request.POST.get('method')
            # type = request.POST.get('type')
            # name = slugify(request.POST.get('name'))


                type = service_form.cleaned_data["type"]
                server = None
                if type == "AUTO":
                    type, server = _verify_service_type(url)

                if type is None:
                    return HttpResponse('Could not determine server type', status = 400)

                if "user" in request.POST and "password" in request.POST:
                    user = request.POST.get('user')
                    password = request.POST.get('password')
                else:
                    user = None
                    password = None

                if type in ["WMS","OWS"]:
                    return _process_wms_service(url, type, user, password, wms=server, owner=request.user)
                elif type == "REST":
                    return _register_arcgis_url(url, user, password, owner=request.user)
                elif type in ["OGP","CSW"]:
                    return _register_harvested_service(type, url, user, password, owner=request.user)
                else:
                    return HttpResponse('Not Implemented (Yet)', status=501)
            except Exception, e:
                logger.error("Unexpected Error", exc_info=1)
                return HttpResponse('Unexpected Error: %s' % e, status=500)
    elif request.method == 'PUT':
        # Update a previously registered Service
        return HttpResponse('Not Implemented (Yet)', status=501)
    elif request.method == 'DELETE':
        # Delete a previously registered Service
        return HttpResponse('Not Implemented (Yet)', status=501)
    else:
        return HttpResponse('Invalid Request', status = 400)

def register_service_by_type(request):
    """
    Register a service based on a specified type
    """
    url = request.POST.get("url")
    type = request.POST.get("type")

    try:
        url = _clean_url(url)
        service = Service.objects.get(base_url=url)
        return
    except:
        type, server = _verify_service_type(url, type)

        if type == "WMS" or type == "OWS":
            return _process_wms_service(url, type, None, None, wms=server)
        elif type == "REST":
            return _register_arcgis_url(url, None, None)

def _is_unique(url):
    """
    Determine if a service is already registered based on matching url
    """
    try:
        service = Service.objects.get(base_url=url)
        return False
    except Service.DoesNotExist:
        return True

def _clean_url(base_url):
    """
    Remove all parameters from a URL
    """
    urlprop = urlsplit(base_url)
    url = urlunsplit((urlprop.scheme, urlprop.netloc, urlprop.path, None, None))
    return url

def _get_valid_name(proposed_name):
    """
    Return a unique slug name for a service
    """
    slug_name = slugify(proposed_name)
    name = slug_name
    if len(slug_name)>40:
        name = slug_name[:40]
    existing_service = Service.objects.filter(name=name)
    iter = 1
    while existing_service.count() > 0:
        name = slug_name + str(iter)
        existing_service = Service.objects.filter(name=name)
        iter+=1
    return name

def _verify_service_type(base_url, type=None):
    """
    Try to determine service type by process of elimination
    """

    if type in ["WMS", "OWS", None]:
        try:
            service = WebMapService(base_url)
            service_type = 'WMS'
            try:
                servicewfs = WebFeatureService(base_url)
                service_type = 'OWS'
            except:
                pass
            return [service_type, service]
        except:
            pass
    if type in ["TMS",None]:
        try:
            service = TileMapService(base_url)
            return ["TMS", service]
        except:
            pass
    if type in ["REST", None]:
        try:
            service = ArcFolder(base_url)
            service.services
            return ["REST", service]
        except:
            pass
    if type in ["CSW", None]:
        try:
            service = CatalogueServiceWeb(base_url)
            return ["CSW", service]
        except:
            pass
    if type in ["OGP", None]:
        #Just use a specific OGP URL for now
        if base_url == settings.OGP_URL:
            return ["OGP", None]
        return None

def _process_wms_service(url, type, username, password, wms=None, owner=None, parent=None):
    """
    Create a new WMS/OWS service, cascade it if necessary
    """
    if wms is None:
        wms = WebMapService(url)
    try:
        base_url = _clean_url(wms.getOperationByName('GetMap').methods['Get']['url'])

        if base_url and base_url != url:
            url = base_url
            wms = WebMapService(base_url)
    except:
        logger.info("Could not retrieve GetMap url, using originally supplied URL %s" % url)
        pass

    try:
        service = Service.objects.get(base_url=url)
        return_dict = [{'status': 'ok',
                    'msg': _("This is an existing service"),
                    'service_id': service.pk,
                    'service_name': service.name,
                    'service_title': service.title
                   }]
        return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
    except:
        pass

    title = wms.identification.title
    if title:
        name = _get_valid_name(title)
    else:
        name = _get_valid_name(urlsplit(url).netloc)
    try:
        supported_crs  = ','.join(wms.contents.itervalues().next().crsOptions)
    except:
        supported_crs = None
    if supported_crs and re.search('EPSG:900913|EPSG:3857', supported_crs):
        return _register_indexed_service(type, url, name, username, password, wms=wms, owner=owner, parent=parent)
    else:
        return _register_cascaded_service(url, type, name, username, password, wms=wms, owner=owner, parent=parent)

def _register_cascaded_service(url, type, name, username, password, wms=None, owner=None, parent=None):
    """
    Register a service as cascading WMS
    """

    try:
        service = Service.objects.get(base_url=url)
        return_dict = {}
        return_dict['service_id'] = service.pk
        return_dict['msg'] = "This is an existing Service"
        return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
    except:
        pass


    if wms is None:
        wms = WebMapService(url)
    # TODO: Make sure we are parsing all service level metadata
    # TODO: Handle for setting ServiceContactRole
    service = Service.objects.create(base_url = url,
        type = type,
        method='C',
        name = name,
        version = wms.identification.version,
        title = wms.identification.title,
        abstract = wms.identification.abstract,
        online_resource = wms.provider.url,
        owner=owner,
        parent = parent)

    service.keywords = ','.join(wms.identification.keywords)
    service.save()

    if type in ['WMS', 'OWS']:
        # Register the Service with GeoServer to be cascaded
        cat = Catalog(settings.GEOSERVER_BASE_URL + "rest", 
                        _user , _password)
        # Can we always assume that it is geonode?
        try:
            cascade_ws = cat.get_workspace(settings.CASCADE_WORKSPACE)
        except FailedRequestError:
            cascade_ws = cat.create_workspace(settings.CASCADE_WORKSPACE, "http://geonode.org/cascade")

        #TODO: Make sure there isn't an existing store with that name, and deal with it if there is

        try:
            ws = cat.get_store(name, cascade_ws)
        except:
            ws = cat.create_wmsstore(name,cascade_ws, username, password)
            ws.capabilitiesURL = url
            ws.type = "WMS"
            cat.save(ws)
        available_resources = ws.get_resources(available=True)


    elif type == 'WFS':
        # Register the Service with GeoServer to be cascaded
        cat = Catalog(settings.GEOSERVER_BASE_URL + "rest", 
                        _user , _password)
        # Can we always assume that it is geonode?
        cascade_ws = cat.get_workspace(settings.CASCADE_WORKSPACE)
        if cascade_ws is None:
            cascade_ws = cat.create_workspace(settings.CASCADE_WORKSPACE, "http://geonode.org/cascade")

        try:
            wfs_ds = cat.get_store(name, cascade_ws)
        except:
            wfs_ds = cat.create_datastore(name, cascade_ws)
            connection_params = {
                "WFSDataStoreFactory:MAXFEATURES": "0",
                "WFSDataStoreFactory:TRY_GZIP": "true",
                "WFSDataStoreFactory:PROTOCOL": "false",
                "WFSDataStoreFactory:LENIENT": "true",
                "WFSDataStoreFactory:TIMEOUT": "3000",
                "WFSDataStoreFactory:BUFFER_SIZE": "10",
                "WFSDataStoreFactory:ENCODING": "UTF-8",
                "WFSDataStoreFactory:WFS_STRATEGY": "nonstrict",
                "WFSDataStoreFactory:GET_CAPABILITIES_URL": url,
            }
            if username and password:
                connection_params["WFSDataStoreFactory:USERNAME"] = username
                connection_params["WFSDataStoreFactory:PASSWORD"] = password

            wfs_ds.connection_parameters = connection_params
            cat.save(wfs_ds)
        available_resources = wfs_ds.get_resources(available=True)
        
        # Save the Service record
        service, created = Service.objects.get_or_create(type = type,
                            method='C',
                            base_url = url,
                            name = name,
                            owner = owner)
        service.save()

    elif type == 'WCS':
        return HttpResponse('Not Implemented (Yet)', status=501)
    else:
        return HttpResponse(
            'Invalid Method / Type combo: ' + 
            'Only Cascaded WMS, WFS and WCS supported',
            mimetype="text/plain",
            status=400)

    message = "Service %s registered" % service.name
    return_dict = [{'status': 'ok',
                    'msg': message,
                    'service_id': service.pk,
                    'service_name': service.name,
                    'service_title': service.title,
                    'available_layers': available_resources
                   }]

    if settings.USE_QUEUE:
        #Create a layer import job
        WebServiceHarvestLayersJob.objects.get_or_create(service=service)
    else:
        _register_cascaded_layers(service)
    return HttpResponse(json.dumps(return_dict),
                        mimetype='application/json',
                        status=200)

def _register_cascaded_layers(service, owner=None):
    """
    Register layers for a cascading WMS
    """
    if service.type == 'WMS' or service.type == "OWS":
        cat = Catalog(settings.GEOSERVER_BASE_URL + "rest", 
                        _user , _password)
        # Can we always assume that it is geonode?
        # Should cascading layers have a separate workspace?
        cascade_ws = cat.get_workspace(settings.CASCADE_WORKSPACE)
        if cascade_ws is None:
            cascade_ws = cat.create_workspace(settings.CASCADE_WORKSPACE, 'cascade')
        try:
            store = cat.get_store(service.name,cascade_ws)
        except Exception:
            store = cat.create_wmsstore(service.name, cascade_ws)
        wms = WebMapService(service.base_url)
        layers = list(wms.contents)

        count = 0
        for layer in layers:
            lyr = cat.get_resource(layer, store, cascade_ws)
            if lyr is None:
                if service.type in ["WMS","OWS"]:
                    resource = cat.create_wmslayer(cascade_ws, store, layer)
                elif service.type == "WFS":
                    resource = cat.create_wfslayer(cascade_ws, store, layer)

                if resource:
                    cascaded_layer, created = Layer.objects.get_or_create(name=resource.name, service=service,
                        defaults = {
                            "workspace": cascade_ws.name,
                            "store": store.name,
                            "storeType": store.resource_type,
                            "typename": "%s:%s" % (cascade_ws.name, resource.name),
                            "title": resource.title or 'No title provided',
                            "abstract": resource.abstract or 'No abstract provided',
                            "owner": None,
                            "uuid": str(uuid.uuid4()),
                            "service": service
                        })


                    if created:
                        cascaded_layer.save()
                        if cascaded_layer is not None and cascaded_layer.bbox is None:
                            cascaded_layer._populate_from_gs(gs_resource=resource)
                        cascaded_layer.set_default_permissions()
                        cascaded_layer.save_to_geonetwork()

                        service_layer, created = ServiceLayer.objects.get_or_create(
                            service=service,
                            typename=cascaded_layer.name
                        )
                        service_layer.layer = cascaded_layer
                        service_layer.title=cascaded_layer.title,
                        service_layer.description=cascaded_layer.abstract,
                        service_layer.styles=cascaded_layer.styles
                        service_layer.save()

                        count += 1
                    else:
                        logger.error("Resource %s from store %s could not be saved as layer" % (layer, store.name))
        message = "%d Layers Registered" % count
        return_dict = {'status': 'ok', 'msg': message }
        return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
    elif service.type == 'WCS':
        return HttpResponse('Not Implemented (Yet)', status=501)
    else:
        return HttpResponse('Invalid Service Type', status=400)

def _register_indexed_service(type, url, name, username, password, verbosity=False, wms=None, owner=None, parent=None):
    """
    Register a service - WMS or OWS currently supported
    """
    if type in ['WMS',"OWS","HGL"]:
        # TODO: Handle for errors from owslib
        if wms is None:
            wms = WebMapService(url)
        # TODO: Make sure we are parsing all service level metadata
        # TODO: Handle for setting ServiceContactRole

        try:
            service = Service.objects.get(base_url=url)
            return_dict = {}
            return_dict['service_id'] = service.pk
            return_dict['msg'] = "This is an existing Service"
            return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
        except:
            pass
        
        
        service = Service.objects.create(base_url = url,
            type = type,
            method='I',
            name = name,
            version = wms.identification.version,
            title = wms.identification.title,
            abstract = wms.identification.abstract,
            online_resource = wms.provider.url,
            owner=owner,
            parent=parent)

        service.keywords = ','.join(wms.identification.keywords)
        service.save()

        available_resources = []
        for layer in list(wms.contents):
                available_resources.append([wms[layer].name, wms[layer].title])

        if settings.USE_QUEUE:
            #Create a layer import job
            WebServiceHarvestLayersJob.objects.get_or_create(service=service)
        else:
            _register_indexed_layers(service, wms=wms)

        message = "Service %s registered" % service.name
        return_dict = [{'status': 'ok',
                       'msg': message,
                       'service_id': service.pk,
                       'service_name': service.name,
                       'service_title': service.title,
                       'available_layers': available_resources
        }]
        return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
    elif type == 'WFS':
        return HttpResponse('Not Implemented (Yet)', status=501)
    elif type == 'WCS':
        return HttpResponse('Not Implemented (Yet)', status=501)
    else:
        return HttpResponse(
            'Invalid Method / Type combo: ' + 
            'Only Indexed WMS, WFS and WCS supported',
            mimetype="text/plain",
            status=400)

def _register_indexed_layers(service, wms=None, verbosity=False):
    """
    Register layers for an indexed service (only WMS/OWS currently supported
    """
    logger.info("Registering layers for %s" % service.base_url)
    if re.match("WMS|OWS", service.type):
        wms = wms or WebMapService(service.base_url)
        count = 0
        for layer in list(wms.contents):
            wms_layer = wms[layer]
            if wms_layer is None or wms_layer.name is None:
                continue
            logger.info("Registering layer %s" % wms_layer.name)
            if verbosity:
                print "Importing layer %s" % layer
            layer_uuid = str(uuid.uuid1())
            try:
                keywords = map(lambda x: x[:100], wms_layer.keywords)
            except:
                keywords = []
            if not wms_layer.abstract:
                abstract = ""
            else:
                abstract = wms_layer.abstract

            srs = None
            ###Some ArcGIS WMSServers indicate they support 900913 but really don't
            if 'EPSG:900913' in wms_layer.crsOptions and "MapServer/WmsServer" not in service.base_url:
                srs = 'EPSG:900913'
            elif len(wms_layer.crsOptions) > 0:
                matches = re.findall('EPSG\:(3857|102100|102113)', ' '.join(wms_layer.crsOptions))
                if matches:
                    srs = 'EPSG:%s' % matches[0]
            if srs is None:
                message = "%d Incompatible projection - try setting the service as cascaded" % count
                return_dict = {'status': 'ok', 'msg': message }
                return HttpResponse(json.dumps(return_dict),
                                mimetype='application/json',
                                status=200)

            llbbox = list(wms_layer.boundingBoxWGS84)
            bbox = llbbox_to_mercator(llbbox)

            # Need to check if layer already exists??
            llbbox = list(wms_layer.boundingBoxWGS84)
            saved_layer, created = Layer.objects.get_or_create(
                service=service,
                typename=wms_layer.name,
                defaults=dict(
                    name=wms_layer.name,
                    store=service.name, #??
                    storeType="remoteStore",
                    workspace="remoteWorkspace",
                    title=wms_layer.title,
                    abstract=abstract,
                    uuid=layer_uuid,
                    owner=None,
                    srs=srs,
                    bbox = bbox,
                    llbbox = llbbox,
                    geographic_bounding_box=bbox_to_wkt(str(llbbox[0]), str(llbbox[1]),
                                                        str(llbbox[2]), str(llbbox[3]), srid="EPSG:4326")
                )
            )
            if created:
                saved_layer.save()
                saved_layer.set_default_permissions()
                saved_layer.keywords.add(*keywords)
                saved_layer.set_layer_attributes()
                saved_layer.save_to_geonetwork()

                service_layer, created = ServiceLayer.objects.get_or_create(
                    service=service,
                    typename=wms_layer.name
                )
                service_layer.layer = saved_layer
                service_layer.title=wms_layer.title,
                service_layer.description=wms_layer.abstract,
                service_layer.styles=wms_layer.styles
                service_layer.save()
            count += 1
        message = "%d Layers Registered" % count
        return_dict = {'status': 'ok', 'msg': message }
        return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
    elif service.type == 'WFS':
        return HttpResponse('Not Implemented (Yet)', status=501)
    elif service.type == 'WCS':
        return HttpResponse('Not Implemented (Yet)', status=501)
    else:
        return HttpResponse('Invalid Service Type', status=400)


def _register_harvested_service(type, url, username, password, csw=None, owner=None):
    """
    Register a CSW or OGP service  - stub only.  Needs to iterate through all layers and register
    the layers and the services they originate from.
    """
    try:
        service = Service.objects.get(base_url=url)
        return_dict = {}
        return_dict['service_id'] = service.pk
        return_dict['msg'] = "This is an existing Service"
        return HttpResponse(json.dumps(return_dict),
                            mimetype='application/json',
                            status=200)
    except:
        pass

    if csw is None:
        csw = CatalogueServiceWeb(url)

    service = Service.objects.create(base_url = url,
                                     type = type,
                                     method='H' if type == 'CSW' else 'O',
                                     name = _get_valid_name(csw.identification.title or url),
                                     title = csw.identification.title,
                                     version = csw.identification.version,
                                     abstract = csw.identification.abstract,
                                     owner=owner)

    service.keywords = ','.join(csw.identification.keywords)
    service.save

    message = "Service %s registered" % service.name
    return_dict = [{'status': 'ok',
                    'msg': message,
                    'service_id': service.pk,
                    'service_name': service.name,
                    'service_title': service.title
                   }]

    if settings.USE_QUEUE:
        #Create a layer import job
        WebServiceHarvestLayersJob.objects.get_or_create(service=service)
    else:
        _harvest_csw(service)

    return HttpResponse(json.dumps(return_dict),
                        mimetype='application/json',
                        status=200)

def _harvest_csw(csw, maxrecords=10):
    stop = 0
    flag = 0

    src = CatalogueServiceWeb(csw.base_url)

    while stop == 0:
        if flag == 0:  # first run, start from 0
            startposition = 0
        else:  # subsequent run, startposition is now paged
            startposition = src.results['nextrecord']

        src.getrecords2(esn='summary', startposition=startposition, maxrecords=maxrecords)

        print src.results


        if src.results['nextrecord'] == 0 \
        or src.results['returned'] == 0 \
        or src.results['nextrecord'] > src.results['matches']:  # end the loop, exhausted all records
            stop = 1
            break

        # harvest each record to destination CSW
        for record in list(src.records):
            #print i
            record = src.records[record]
            known_types = {}
            print record
            for ref in record.references:
                if ref["scheme"] == "OGC:WMS" or "service=wms&request=getcapabilities" in ref["url"].lower():
                    print "WMS:%s" % ref["url"]
                    known_types["WMS"] = ref["url"]
                if ref["scheme"] == "OGC:WFS" or "service=wfs&request=getcapabilities" in ref["url"].lower():
                    print "WFS:%s" % ref["url"]
                    known_types["WFS"] = ref["url"]
                if ref["scheme"] == "ESRI":
                    print "ESRI:%s" % ref["url"]
                    known_types["REST"] = ref["url"]

            if "WMS" in known_types:
                type = "OWS" if "WFS" in known_types else "WMS"
                try:
                    _process_wms_service(known_types["WMS"], type, None, None, parent=csw)
                except Exception, e:
                    logger.error("Error registering %s:%s" % (known_types["WMS"], str(e)))
            elif "REST" in known_types:
                try:
                    _register_arcgis_url(ref["url"], None, None, parent=csw)
                except Exception, e:
                    logger.error("Error registering %s:%s" % (known_types["REST"], str(e)))
            #source = '%s?service=CSW&version=2.0.2&request=GetRecordById&id=%s' % (service.url, i)

            #dest.harvest(source=source, resourcetype='http://www.isotc211.org/2005/gmd')

        flag = 1
        stop = 0



def _register_arcgis_url(url,username, password, owner=None, parent=None):
    """
    Register an ArcGIS REST service URL
    """
    #http://maps1.arcgisonline.com/ArcGIS/rest/services

    baseurl = _clean_url(url)
    if re.search("\/MapServer\/*(f=json)*", baseurl):
        #This is a MapService
        arcserver = ArcMapService(baseurl)
        return_json = [_process_arcgis_service(arcserver, owner=owner, parent=parent)]

    else:
        #This is a Folder
        arcserver = ArcFolder(baseurl)
        return_json = _process_arcgis_folder(arcserver, services=[], owner=owner, parent=parent)

    return HttpResponse(json.dumps(return_json),
                        mimetype='application/json',
                        status=200)

def _register_arcgis_layers(service, arc=None):
    """
    Register layers from an ArcGIS REST service
    """
    arc = arc or ArcMapService(service.base_url)
    for layer in arc.layers:
        count = 0
        layer_uuid = str(uuid.uuid1())
        layer_bbox = [layer.extent.xmin, layer.extent.ymin, layer.extent.xmax, layer.extent.ymax]
        llbbox =  mercator_to_llbbox(layer_bbox)
        # Need to check if layer already exists??
        saved_layer, created = Layer.objects.get_or_create(
            service=service,
            typename=layer.id,
            defaults=dict(
                name=layer.id,
                store=service.name, #??
                storeType="remoteStore",
                workspace="remoteWorkspace",
                title=layer.name,
                abstract=layer._json_struct['description'],
                uuid=layer_uuid,
                owner=None,
                srs="EPSG:%s" % layer.extent.spatialReference.wkid,
                bbox = layer_bbox,
                llbbox = llbbox,
                geographic_bounding_box=bbox_to_wkt(str(llbbox[0]), str(llbbox[1]),
                                                    str(llbbox[2]), str(llbbox[3]), srid="EPSG:4326" )
            )
        )
        if created:
            saved_layer.set_default_permissions()
            saved_layer.save()
            saved_layer.save_to_geonetwork()

            service_layer, created = ServiceLayer.objects.get_or_create(
                service=service,
                typename=layer.id
            )
            service_layer.layer = saved_layer
            service_layer.title=layer.name,
            service_layer.description=saved_layer.abstract,
            service_layer.styles=None
            service_layer.save()
        count += 1
    message = "%d Layers Registered" % count
    return_dict = {'status': 'ok', 'msg': message }
    return HttpResponse(json.dumps(return_dict),
                        mimetype='application/json',
                        status=200)

def _process_arcgis_service(arcserver, owner=None, parent=None):
    """
    Create a Service model instance for an ArcGIS REST service
    """
    arc_url = _clean_url(arcserver.url)
    try:
        service = Service.objects.get(base_url=arc_url)
        return_dict = {}
        return_dict['service_id'] = service.pk
        return_dict['msg'] = "This is an existing Service"
        return service.base_url
    except:
        pass

    name = _get_valid_name(arcserver.mapName or arc_url)
    service = Service.objects.create(base_url = arc_url, name=name,
        type = 'REST',
        method='I',
        title = arcserver.mapName,
        abstract = arcserver.serviceDescription,
        online_resource = arc_url,
        owner=owner,
        parent=parent)

    available_resources = []
    for layer in list(arcserver.layers):
        available_resources.append([layer.id, layer.name])

    if settings.USE_QUEUE:
        #Create a layer import job
        WebServiceHarvestLayersJob.objects.get_or_create(service=service)
    else:
        _register_arcgis_layers(service, arc=arcserver)

    message = "Service %s registered" % service.name
    return_dict = {'status': 'ok',
                       'msg': message,
                       'service_id': service.pk,
                       'service_name': service.name,
                       'service_title': service.title,
                       'available_layers': available_resources
        }
    return return_dict

def _process_arcgis_folder(folder, services=[], owner=None, parent=None):
    """
    Iterate through folders and services in an ArcGIS REST service folder
    """
    for service in folder.services:
        if  isinstance(service,ArcMapService) and service.spatialReference.wkid in [102100,3857,900913]:
            print "Base URL is %s" % service.url
            result_json = _process_arcgis_service(service, owner, parent=parent)
            services.append(result_json)
        else:
            return_dict = {}
            return_dict['msg'] =  _("Could not find any layers in a compatible projection:") + service.url
            services.append(return_dict)
    for subfolder in folder.folders:
        _process_arcgis_folder(subfolder, services, owner)
    return services

def _harvest_ogp_service(url, num_rows=100, start=0,owner=None):
    base_query_str =  "?q=_val_:%22sum(sum(product(9.0,map(sum(map(MinX,-180.0,180,1,0)," +  \
        "map(MaxX,-180.0,180.0,1,0),map(MinY,-90.0,90.0,1,0),map(MaxY,-90.0,90.0,1,0)),4,4,1,0))),0,0)%22" + \
        "&debugQuery=false&&fq={!frange+l%3D1+u%3D10}product(2.0,map(sum(map(sub(abs(sub(0,CenterX))," + \
        "sum(171.03515625,HalfWidth)),0,400000,1,0),map(sub(abs(sub(0,CenterY)),sum(75.84516854027,HalfHeight))," + \
        "0,400000,1,0)),0,0,1,0))&wt=json&fl=Name,CollectionId,Institution,Access,DataType,Availability," + \
        "LayerDisplayName,Publisher,GeoReferenced,Originator,Location,MinX,MaxX,MinY,MaxY,ContentDate,LayerId," + \
        "score,WorkspaceName,SrsProjectionCode&sort=score+desc&fq=DataType%3APoint+OR+DataType%3ALine+OR+" + \
        "DataType%3APolygon+OR+DataType%3ARaster+OR+DataType%3APaper+Map&fq=Access:Public"

    #base_query_str += "&fq=Institution%3AHarvard"

    service, created = Service.objects.get_or_create(base_url = url)
    if created:
        service.type = type
        service.method='O'
        service.name = "OpenGeoPortal"
        service.abstract = OGP_ABSTRACT
        service.owner=owner
        service.save()


    fullurl = service.url + base_query_str + ("&rows=%d&start=%d" % (num_rows, start))
    response = urllib.urlopen(fullurl).read()
    json_response = json.loads(response)
    result_count =  json_response["response"]["numFound"]
    process_ogp_results(service,json_response)

    while start < result_count:
        start = start + num_rows
        _harvest_ogp_service(service, num_rows, start)

def process_ogp_results(service,result_json, owner=None):
    for doc in result_json["response"]["docs"]:
        try:
            locations = json.loads(doc["Location"])
        except:
            continue
        if "tilecache" in locations:
            service_url = locations["tilecache"][0]
            service_type = "WMS"
        elif "wms" in locations:
            service_url = locations["wms"][0]
            if "wfs" in locations:
                service_type = "OWS"
            else:
                service_type = "WMS"
        else:
            pass

        #Harvard is a special case
        if doc["Institution"] == "Harvard":
            service_type = "HGL"

        service = None
        try:
            service = Service.objects.get(base_url=service_url)
        except Service.DoesNotExist:
            if service_type in ["WMS","OWS", "HGL"]:
                try:
                    response = _process_wms_service(service_url, service_type, None, None, parent=service)
                    r_json = json.loads(response.content)
                    service = Service.objects.get(id=r_json[0]["service_id"])
                except Exception, e:
                    print str(e)

        if service:
                typename = doc["Name"]
                if service_type == "HGL":
                    typename = typename.replace("SDE.","")
                elif doc["WorkspaceName"]:
                    typename = doc["WorkspaceName"] + ":" + typename


                bbox = (
                    float(doc['MinX']),
                    float(doc['MinY']),
                    float(doc['MaxX']),
                    float(doc['MaxY']),
                )

                layer_uuid = str(uuid.uuid1())
                saved_layer, created = Layer.objects.get_or_create(service=service, typename=typename,
                    defaults=dict(
                    name=doc["Name"],
                    service = service,
                    uuid=layer_uuid,
                    store=service.name, #??
                    storeType="remoteStore",
                    workspace=doc["WorkspaceName"],
                    title=doc["LayerDisplayName"],
                    owner=None,
                    srs="EPSG:900913", #Assumption
                    bbox = llbbox_to_mercator(list(bbox)),
                    llbbox = list(bbox),
                    geographic_bounding_box=bbox_to_wkt(str(bbox[0]), str(bbox[1]),
                                                        str(bbox[2]), str(bbox[3]), srid="EPSG:4326" )
                    )
                )
                saved_layer.set_default_permissions()
                saved_layer.save()
                saved_layer.save_to_geonetwork()
                service_layer, created = ServiceLayer.objects.get_or_create(service=service,typename=typename,
                                                                            defaults=dict(
                                                                                title=doc["LayerDisplayName"]
                                                                            )
                )
                if service_layer.layer is None:
                    service_layer.layer = saved_layer
                    service_layer.save()

def service_detail(request, service_id):
    '''
    This view shows the details of a service 
    '''
    service = get_object_or_404(Service,pk=service_id)
    layers = Layer.objects.filter(service=service)
    services = service.service_set.all()
    return render_to_response("services/service_detail.html", RequestContext(request, {
        'service': service,
        'layers': layers,
        'services' : services,
        'permissions_json': json.dumps(_perms_info(service, SERVICE_LEV_NAMES))
    }))

@login_required
def edit_service(request, service_id):
    """
    Edit an existing Service
    """
    service_obj = get_object_or_404(Service,pk=service_id)


    if request.method == "POST":
        service_form = ServiceForm(request.POST, instance=service_obj, prefix="service")
        if service_form.is_valid():
            service_obj = service_form.save(commit=False)
            service_obj.keywords.clear()
            service_obj.keywords.add(*service_form.cleaned_data['keywords'])
            service_obj.save()

            return HttpResponseRedirect(service_obj.get_absolute_url())
    else:
        service_form = ServiceForm(instance=service_obj, prefix="service")


    return render_to_response("services/service_edit.html", RequestContext(request, {
                "service": service_obj,
                "service_form": service_form
            }))

def update_layers(service):
    """
    Import/update layers for an existing service
    """
    if service.method == "C":
        _register_cascaded_layers(service)
    elif service.type in ["WMS","OWS"]:
        _register_indexed_layers(service)
    elif service.type in ["REST"]:
        _register_arcgis_layers(service)

@login_required
def remove_service(request, service_id):
    '''
    Delete a service, and its constituent layers. 
    '''
    service_obj = get_object_or_404(Service,pk=service_id)

    if not request.user.has_perm('maps.delete_service', obj=service_obj):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to remove this service.")})), status=401)

    if request.method == 'GET':
        return render_to_response("services/service_remove.html", RequestContext(request, {
            "service": service_obj
        }))
    elif request.method == 'POST':
        # servicelayers = service_obj.servicelayer_set.all()
        # for servicelayer in servicelayers:
        #     servicelayer.delete()
        #
        # layers = service_obj.layer_set.all()
        # for layer in layers:
        #     layer.delete()
        service_obj.delete()

        return HttpResponseRedirect(reverse("services"))

def set_service_permissions(service, perm_spec):
    if "authenticated" in perm_spec:
        service.set_gen_level(AUTHENTICATED_USERS, perm_spec['authenticated'])
    if "anonymous" in perm_spec:
        service.set_gen_level(ANONYMOUS_USERS, perm_spec['anonymous'])
    users = [n for (n, p) in perm_spec['users']]
    service.get_user_levels().exclude(user__username__in = users + [service.owner]).delete()
    for username, level in perm_spec['users']:
        user = User.objects.get(username=username)
        service.set_user_level(user, level)

@login_required
def ajax_service_permissions(request, service_id):
    service = get_object_or_404(Service,pk=service_id) 
    if not request.user.has_perm("maps.change_service_permissions", obj=service):
        return HttpResponse(
            'You are not allowed to change permissions for this service',
            status=401,
            mimetype='text/plain'
        )

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing service permissions',
            status=405,
            mimetype='text/plain'
        )

    spec = json.loads(request.raw_post_data)
    set_service_permissions(service, spec)

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain')
