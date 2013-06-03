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

from django.utils.translation import ugettext as _
from django.conf import settings

# implicitly defined 'generic' groups of users
ANONYMOUS_USERS = 'anonymous'
AUTHENTICATED_USERS = 'authenticated'
CUSTOM_GROUP_USERS = 'customgroup'

GENERIC_GROUP_NAMES = {
    ANONYMOUS_USERS: _('Anonymous Users'),
    AUTHENTICATED_USERS: _('Registered Users'),
    CUSTOM_GROUP_USERS: _(settings.CUSTOM_GROUP_NAME)
}


INVALID_PERMISSION_MESSAGE = _("Invalid permission level.")