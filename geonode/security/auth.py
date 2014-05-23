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
import datetime

from django.contrib.auth.backends import ModelBackend
from django.conf import settings
from django.contrib.contenttypes.models import ContentType 
from django.db import models
from django.contrib.auth.models import User
from geonode.security.models import GenericObjectRoleMapping, Permission, \
    UserObjectRoleMapping
if "geonode.contrib.groups" in settings.INSTALLED_APPS:
    from geonode.security.models import GroupObjectRoleMapping
    from geonode.contrib.groups.models import Group
from geonode.security.enumerations import ANONYMOUS_USERS, AUTHENTICATED_USERS, CUSTOM_GROUP_USERS


class GranularBackend(ModelBackend):
    """
    A granular permissions backend that supports row-level 
    user permissions via roles. 
    """
    
    supports_object_permissions = True
    supports_anonymous_user = True

    def get_group_permissions(self, user_obj, obj=None):
        """
        Returns a set of permission strings that this user has through his/her
        groups.
        """
        if obj is None:
            return ModelBackend.get_group_permissions(self, user_obj)
        else:
            return set() # not implemented

    def get_all_permissions(self, user_obj, obj=None):
        """
        """

        if obj is None:
            return ModelBackend.get_all_permissions(self, user_obj)
        else:
            # does not handle objects that are not in the database.
            if not isinstance(obj, models.Model):
                return set()
            
            if not hasattr(user_obj, '_obj_perm_cache'):
                # TODO: this cache should really be bounded.
                # repoze.lru perhaps?
                user_obj._obj_perm_cache = dict()
            try:
                obj_key = self._cache_key_for_obj(obj)
                return user_obj._obj_perm_cache[obj_key]
            except KeyError:
                all_perms = ['%s.%s' % p for p in self._get_all_obj_perms(user_obj, obj)]
                user_obj._obj_perm_cache[obj_key] = all_perms
                return all_perms

    def has_perm(self, user_obj, perm, obj=None):
        if obj is None:
            # fallback to Django default permission backend
            return ModelBackend.has_perm(self, user_obj, perm)
        else:
            # in case the user is the owner, he/she has always permissions, 
            # otherwise we need to check
            if hasattr(obj, 'owner') and user_obj == obj.owner:
                return True
            else:
                return perm in self.get_all_permissions(user_obj, obj=obj)

    def _cache_key_for_obj(self, obj):
        model = obj.__class__
        opts = model._meta
        while opts.proxy:
            model = opts.proxy_for_model
            opts = model._meta
        key = (opts.app_label, opts.object_name.lower(), obj.id)
        return key
    
        
    def _get_generic_obj_perms(self, generic_roles, obj):
        perms = set()
        ct = ContentType.objects.get_for_model(obj)
        for rm in GenericObjectRoleMapping.objects.select_related('role', 'role__permissions', 'role__permissions__content_type').filter(object_id=obj.id, object_ct=ct, subject__in=generic_roles).all():
            for perm in rm.role.permissions.all():
                perms.add((perm.content_type.app_label, perm.codename))
        return perms


    def _get_all_obj_perms(self, user_obj, obj):
        """
        get all permissions for user in the context of ob (not cached)
        """
        obj_perms = set()
        generic_roles = [ANONYMOUS_USERS]
        if not user_obj.is_anonymous():
            generic_roles.append(AUTHENTICATED_USERS)
            profile = user_obj.get_profile()
        obj_perms.update(self._get_generic_obj_perms(generic_roles, obj))
        
        ct = ContentType.objects.get_for_model(obj)

        if settings.CUSTOM_AUTH["enabled"]:
            profile = user_obj.get_profile()
            if profile and profile.is_org_member and profile.member_expiration_dt >= datetime.today().date():
                generic_roles.append(CUSTOM_GROUP_USERS)

        if not user_obj.is_anonymous():
            for rm in UserObjectRoleMapping.objects.select_related('role', 'role__permissions', 'role__permissions__content_type').filter(object_id=obj.id, object_ct=ct, user=user_obj).all():
                for perm in rm.role.permissions.all():
                    obj_perms.add((perm.content_type.app_label, perm.codename))
            if "geonode.contrib.groups" in settings.INSTALLED_APPS:
                groups = Group.groups_for_user(user_obj)
                for group in groups:
                    for rm in GroupObjectRoleMapping.objects.select_related('role', 'role__permissions', 'role__permissions__content_type').filter(object_id=obj.id, object_ct=ct, group=group).all():
                        for perm in rm.role.permissions.all():
                            obj_perms.add((perm.content_type.app_label, perm.codename))

        return obj_perms

    def objects_with_perm(self, acl_obj, perm, ModelType):
        """
        select identifiers of objects the type specified that the 
        user or group specified has the permission 'perm' for.
        """

        if not isinstance(perm, Permission):
            perm = self._permission_for_name(perm)
        ct = ContentType.objects.get_for_model(ModelType)
        
        obj_ids = set()
        generic_roles = [ANONYMOUS_USERS]

        if isinstance(acl_obj, User):
            if not acl_obj.is_anonymous():
                generic_roles.append(AUTHENTICATED_USERS)
                obj_ids.update([x[0] for x in UserObjectRoleMapping.objects.filter(user=acl_obj,
                                                                                   role__permissions=perm,
                                                                                   object_ct=ct).values_list('object_id')])

                if settings.CUSTOM_AUTH["enabled"]:
                    profile = acl_obj.get_profile()
                    if profile and profile.is_org_member:
                        generic_roles.append(CUSTOM_GROUP_USERS)

                if "geonode.contrib.groups" in settings.INSTALLED_APPS:
                    # If the user is a member of any groups, see if the groups have permission to the object.
                    for group in Group.groups_for_user(acl_obj):
                        obj_ids.update([x[0] for x in GroupObjectRoleMapping.objects.filter(group=group,
                                                                                            role__permissions=perm,
                                                                                            object_ct=ct).values_list('object_id')])

        if "geonode.contrib.groups" in settings.INSTALLED_APPS:
            if isinstance(acl_obj, Group):
                obj_ids.update([x[0] for x in GroupObjectRoleMapping.objects.filter(group=acl_obj,
                                                                                    role__permissions=perm,
                                                                                    object_ct=ct).values_list('object_id')])
           

        obj_ids.update([x[0] for x in GenericObjectRoleMapping.objects.filter(subject__in=generic_roles, 
                                                                              role__permissions=perm,
                                                                              object_ct=ct).values_list('object_id')])
    
        return obj_ids

    def _permission_for_name(self, perm):
        ps = perm.index('.')
        app_label = perm[0:ps]
        codename = perm[ps+1:]
        return Permission.objects.get(content_type__app_label=app_label, codename=codename)
