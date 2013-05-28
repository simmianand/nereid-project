# -*- coding: utf-8 -*-
'''
    nereid_project

    :copyright: (c) 2010-2013 by Openlabs Technologies & Consulting (P) Ltd.
    :license: GPLv3, see LICENSE for more details

'''
import project
import company
import iteration
from trytond.pool import Pool

from project import WebSite, ProjectUsers, ProjectInvitation, \
    ProjectWorkInvitation, Project, Tag, TaskTags, \
    ProjectHistory, ProjectWorkCommit
from company import Company, CompanyProjectAdmins, NereidUser
from iteration import ProjectIteration, Work


def register():
    """This function will register trytond module project_billing
    """
    Pool.register(
        WebSite,
        ProjectUsers,
        ProjectInvitation,
        ProjectWorkInvitation,
        Project,
        Tag,
        TaskTags,
        ProjectHistory,
        ProjectWorkCommit,
        Company,
        CompanyProjectAdmins,
        NereidUser,
        ProjectIteration,
        Work,
        module='nereid_project', type_='model',
    )
