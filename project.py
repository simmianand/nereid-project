# -*- coding: utf-8 -*-
"""
    project

    Extend the project to allow users

    :copyright: (c) 2012-2013 by Openlabs Technologies & Consulting (P) Limited
    :license: GPLv3, see LICENSE for more details.
"""
import uuid
import re
import tempfile
import random
import string
import json
import warnings
import time
import dateutil
import calendar
from collections import defaultdict
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from itertools import chain, cycle
from mimetypes import guess_type
from email.utils import parseaddr

from babel.dates import parse_date
from nereid import (request, abort, render_template, login_required, url_for,
    redirect, flash, jsonify, render_email, permissions_required)
from flask import send_file
from nereid.ctx import has_request_context
from nereid.signals import registration
from nereid.contrib.pagination import Pagination
from trytond.model import ModelView, ModelSQL, fields
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction
from trytond.pyson import Eval
from trytond.config import CONFIG
from trytond.tools import get_smtp_server, datetime_strftime
from trytond.backend import TableHandler

__all__ = ['WebSite', 'ProjectUsers', 'ProjectInvitation',
    'TimesheetEmployeeDay', 'ProjectWorkInvitation', 'Project', 'Tag',
    'TaskTags', 'ProjectHistory', 'ProjectWorkCommit']
__metaclass__ = PoolMeta


calendar.setfirstweekday(calendar.SUNDAY)
PROGRESS_STATES = [
    ('Backlog', 'Backlog'),
    ('Planning', 'Planning'),
    ('In Progress', 'In Progress'),
    ('Review', 'Review/QA'),
]


class WebSite:
    """
    Website
    """
    __name__ = "nereid.website"

    @classmethod
    @login_required
    def home(cls):
        """
        Put recent projects into the home
        """
        return redirect(url_for('project.work.home'))


class ProjectUsers(ModelSQL):
    'Project Users'
    __name__ = 'project.work-nereid.user'

    project = fields.Many2One(
        'project.work', 'Project',
        ondelete='CASCADE', select=True, required=True
    )

    user = fields.Many2One(
        'nereid.user', 'User', select=1, required=True
    )

    @classmethod
    def __register__(cls, module_name):
        '''
        Register class and update table name to new.
        '''
        cursor = Transaction().cursor
        table  = TableHandler(cursor, cls, module_name)
        super(ProjectUsers, cls).__register__(module_name)
        # Migration
        if table.table_exist(cursor, 'project_work_nereid_user_rel'):
            table.table_rename(
                cursor, 'project_work_nereid_user_rel',
                'project_work-nereid_user'
            )


class ProjectInvitation(ModelSQL, ModelView):
    "Project Invitation store"
    __name__ = 'project.work.invitation'

    email = fields.Char('Email', required=True, select=True)
    invitation_code = fields.Char(
        'Invitation Code', select=True
    )
    nereid_user = fields.Many2One('nereid.user', 'Nereid User')
    project = fields.Many2One('project.work', 'Project')

    joining_date = fields.Function(
        fields.DateTime('Joining Date', depends=['nereid_user']),
        'get_joining_date'
    )

    @classmethod
    def __setup__(cls):
        super(ProjectInvitation, cls).__setup__()
        cls._sql_constraints += [
            (
                'invitation_code_unique',
                'UNIQUE(invitation_code)',
                'Invitation code cannot be same',
            ),
        ]

    @staticmethod
    def default_invitation_code():
        """
        Sets the default invitation code as uuid everytime
        """
        return unicode(uuid.uuid4())

    def get_joining_date(self, name):
        """
        Joining Date of User
        """
        if self.nereid_user:
            return self.nereid_user.create_date

    @login_required
    def remove_invite(self):
        """
        Remove the invite to a participant from project
        """
        # Check if user is among the project admins
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to remove invited users." +
                " Contact your project admin for the same.")
            return redirect(request.referrer)

        if request.method == 'POST':
            self.delete([self])

            if request.is_xhr:
                return jsonify({
                    'success': True,
                })

            flash("Invitation to the user has been voided."
                "The user can no longer join the project unless reinvited")
        return redirect(request.referrer)

    @login_required
    def resend_invite(self):
        """Resend the invite to a participant
        """
        # Check if user is among the project admins
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to resend invites. \
                Contact your project admin for the same.")
            return redirect(request.referrer)

        if request.method == 'POST':
            subject = '[%s] You have been re-invited to join the project' \
                % self.project.name
            email_message = render_email(
                text_template='project/emails/invite_2_project_text.html',
                subject=subject, to=self.email,
                from_email=CONFIG['smtp_from'], project=self.project,
                invitation=self
            )
            server = get_smtp_server()
            server.sendmail(CONFIG['smtp_from'], [self.email],
                email_message.as_string())
            server.quit()

            if request.is_xhr:
                return jsonify({
                    'success': True,
                })

            flash("Invitation has been resent to %s." % self.email)
        return redirect(request.referrer)


class ProjectWorkInvitation(ModelSQL):
    "Project Work Invitation"
    __name__ = 'project.work-project.invitation'

    invitation = fields.Many2One(
        'project.work.invitation', 'Invitation',
        ondelete='CASCADE', select=1, required=True
    )
    project = fields.Many2One(
        'project.work.invitation', 'Project',
        ondelete='CASCADE', select=1, required=True
    )


class TimesheetEmployeeDay(ModelView):
    'Gantt dat view generator'
    __name__ = 'timesheet_by_employee_by_day'

    employee = fields.Many2One('company.employee', 'Employee')
    date = fields.Date('Date')
    hours = fields.Float('Hours', digits=(16, 2))

    @classmethod
    def __register__(cls, module_name):
        """
        Init Method

        :param module_name: Name of the module
        """
        super(TimesheetEmployeeDay, cls).__register__(module_name)

        query = '"timesheet_by_employee_by_day" AS ' \
                'SELECT timesheet_line.employee, timesheet_line.date, ' \
                'SUM(timesheet_line.hours) AS sum ' \
                'FROM "timesheet_line" ' \
                'GROUP BY timesheet_line.date, timesheet_line.employee;'

        if CONFIG['db_type'] == 'postgres':
            Transaction().cursor.execute('CREATE OR REPLACE VIEW ' + query)

        elif CONFIG['db_type'] == 'sqlite':
            Transaction().cursor.execute('CREATE VIEW IF NOT EXISTS ' + query)


class Project:
    """
    Tryton itself is very flexible in allowing multiple layers of Projects and
    sub projects. But having this and implementing this seems to be too
    convulted for everyday use. So nereid simplifies the process to:

    - Project::Associated to a party
       |
       |-- Task (Type is task)
    """
    __name__ = 'project.work'

    history = fields.One2Many('project.work.history', 'project',
        'History', readonly=True
    )
    participants = fields.Many2Many(
        'project.work-nereid.user', 'project', 'user',
        'Participants'
    )

    tags_for_projects = fields.One2Many('project.work.tag', 'project',
        'Tags', states={
            'invisible': Eval('type') != 'project',
            'readonly': Eval('type') != 'project',
        }
    )

    #: Tags for tasks.
    tags = fields.Many2Many(
        'project.work-project.work.tag', 'task', 'tag',
        'Tags', depends=['type'],
        states={
            'invisible': Eval('type') != 'task',
            'readonly': Eval('type') != 'task',
        }
    )

    created_by = fields.Many2One('nereid.user', 'Created by')

    all_participants = fields.Function(
        fields.Many2Many(
            'project.work-nereid.user', 'project', 'user',
            'All Participants', depends=['company']
        ), 'get_all_participants'
    )
    assigned_to = fields.Many2One(
        'nereid.user', 'Assigned to', depends=['all_participants'],
        domain=[('id', 'in', Eval('all_participants'))],
        states={
            'invisible': Eval('type') != 'task',
            'readonly': Eval('type') != 'task',
        }
    )

    #: Get all the attachments on the object and return them
    attachments = fields.Function(
        fields.One2Many('ir.attachment', None, 'Attachments'),
        'get_attachments'
    )

    progress_state = fields.Selection(
        PROGRESS_STATES, 'Progress State',
        depends=['state', 'type'], select=True,
        states={
            'invisible': (Eval('type') != 'task') | (Eval('state') != 'opened'),
            'readonly': (Eval('type') != 'task') | (Eval('state') != 'opened'),
        }
    )

    repo_commits = fields.One2Many(
        'project.work.commit', 'project', 'Repo Commits'
    )

    @staticmethod
    def default_progress_state():
        '''
        Default for progress state
        '''
        return 'Backlog'

    @classmethod
    @login_required
    def home(cls):
        """
        Put recent projects into the home
        """
        if request.nereid_user.is_project_admin():
            # Display all projects to project admin
            projects = cls.search([
                ('type', '=', 'project'),
                ('parent', '=', False),
            ])
        else:
            projects = cls.search([
                ('participants', '=', request.nereid_user.id),
                ('type', '=', 'project'),
                ('parent', '=', False),
            ])
        if request.is_xhr:
            return jsonify({
                'itemCount': len(projects),
                'items': map(lambda project: project.serialize(), projects),
            })
        return render_template('project/home.jinja', projects=projects)

    def serialize(self):
        """
        Serialize a record, which could be a task or project

        """
        assigned_to = None
        if self.assigned_to:
            assigned_to = {
                'id': self.assigned_to.id,
                'display_name': self.assigned_to.display_name,
            }

        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'parent': self.parent and self.parent.id or None,
            # Task specific
            'tags': map(lambda t: t.name, self.tags),
            'assigned_to': assigned_to,
            'attachments': len(self.attachments),
            'progress_state': self.progress_state,
            'comment': self.comment,
            'effort': self.effort,
            'total_effort': self.total_effort,
            'constraint_finish_time': self.constraint_finish_time and \
                self.constraint_finish_time.isoformat() or None,
        }

    @classmethod
    def rst_to_html(cls):
        """
        Return the response as rst converted to html
        """
        text = request.form['text']
        return render_template('project/rst_to_html.jinja', text=text)

    def get_attachments(self, name):
        """
        Return all the attachments in the object
        """
        Attachment = Pool().get('ir.attachment')

        return map(
            int, Attachment.search([
                ('resource', '=', '%s,%d' % (self.__name__, self.id))
            ])
        )

    @classmethod
    def get_all_participants(cls, works, name):
        """
        All participants includes the participants in the project and also
        the admins
        """
        vals = {}
        for work in works:
            vals[work.id] = []
            vals[work.id].extend([p.id for p in work.participants])
            vals[work.id].extend([p.id for p in work.company.project_admins])
            if work.parent:
                vals[work.id].extend(
                    [p.id for p in work.parent.all_participants]
                )
            vals[work.id] = list(set(vals[work.id]))
        return vals

    @classmethod
    def create(cls, values):
        '''
        Create a Project.

        :param values: Values to create project.
        '''
        if has_request_context():
            values['created_by'] = request.nereid_user.id
            if values['type'] == 'task':
                values.setdefault('participants', [])
                values['participants'].append(
                    ('add', [request.nereid_user.id])
                )
        else:
            # TODO: identify the nereid user through employee
            pass
        return super(Project, cls).create(values)

    def can_read(self, user):
        """
        Returns true if the given nereid user can read the project

        :param user: The browse record of the current nereid user
        """
        if user.is_project_admin():
            return True
        if not user in self.participants:
            raise abort(404)
        return True

    def can_write(self, user):
        """
        Returns true if the given user can write to the project

        :param user: The browse record of the current nereid user
        """
        if user.is_project_admin():
            return True
        if not user in self.participants:
            raise abort(404)
        return True

    @classmethod
    def get_project(cls, project_id):
        """
        Common base for fetching the project while validating if the user
        can use it.

        :param project_id: Project Id of project to fetch.
        """
        projects = cls.search([
            ('id', '=', project_id),
            ('type', '=', 'project'),
        ])

        if not projects:
            raise abort(404)

        if not projects[0].can_read(request.nereid_user):
            # If the user is not allowed to access this project then dont let
            raise abort(404)

        return projects[0]

    @classmethod
    def get_task(cls, task_id):
        """
        Common base for fetching the task while validating if the user
        can use it.

        :param task_id: Task Id of project to fetch.
        """
        tasks = cls.search([
            ('id', '=', task_id),
            ('type', '=', 'task'),
        ])

        if not tasks:
            raise abort(404)

        if not tasks[0].parent.can_write(request.nereid_user):
            # If the user is not allowed to access this project then dont let
            raise abort(403)

        return tasks[0]

    @classmethod
    def get_tasks_by_tag(cls, tag_id):
        """
        Return the tasks associated with a tag

        :param tag_id: tag Id of which tasks to fetch.
        """
        TaskTags = Pool().get('project.work-project.work.tag')

        tasks = map(int, TaskTags.search([
            ('tag', '=', tag_id),
            ('task.state', '=', 'opened'),
        ]))
        return tasks

    @classmethod
    @login_required
    def render_project(cls, project_id):
        """
        Renders a project

        :param project_id: Project Id of project to render.
        """
        # TODO: Convert to instance method
        project = cls.get_project(project_id)
        return render_template(
            'project/project.jinja', project=project, active_type_name="recent"
        )

    @classmethod
    @login_required
    def create_project(cls):
        """Create a new project

        POST will create a new project
        """
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to create new projects." +
            " Contact your project admin for the same.")
            return redirect(request.referrer)

        if request.method == 'POST':
            project = cls.create({
                'name': request.form['name'],
                'type': 'project',
            })
            flash("Project successfully created.")
            return redirect(
                url_for('project.work.render_project', project_id=project.id)
            )

        flash("Could not create project. Try again.")
        return redirect(request.referrer)

    @login_required
    def create_task(self):
        """Create a new task for the specified project

        POST will create a new task
        """
        NereidUser = Pool().get('nereid.user')

        project = self.get_project(self.id)

        # Check if user is among the participants
        self.can_write(request.nereid_user)

        if request.method == 'POST':
            data = {
                'parent': self.id,
                'name': request.form['name'],
                'type': 'task',
                'comment': request.form.get('description', False),
                'tags': [('set',
                    request.form.getlist('tags', int)
                )]
            }

            constraint_start_time = request.form.get(
                'constraint_start_time', False)
            constraint_finish_time = request.form.get(
                'constraint_finish_time', False)
            if constraint_start_time:
                data['constraint_start_time'] = datetime.strptime(
                    constraint_start_time, '%m/%d/%Y')
            if constraint_finish_time:
                data['constraint_finish_time'] = datetime.strptime(
                    constraint_finish_time, '%m/%d/%Y')

            task = self.create(data)

            email_receivers = [p.email for p in self.all_participants]
            if request.form.get('assign_to', False):
                assignee = NereidUser(request.form.get('assign_to', type=int))
                self.write([task], {
                    'assigned_to': assignee.id,
                    'participants': [
                        ('add', [assignee.id])
                    ]
                })
                email_receivers = [assignee.email]
            flash("Task successfully added to project %s" % self.name)
            task.send_mail(email_receivers)
            return redirect(
                url_for('project.work.render_task',
                    project_id=self.id, task_id=task.id
                )
            )

        flash("Could not create task. Try again.")
        return redirect(request.referrer)

    @login_required
    def edit_task(self):
        """
        Edit the task
        """
        task = self.get_task(self.id)

        self.write([task], {
            'name': request.form.get('name'),
            'comment': request.form.get('comment')
        })

        if request.is_xhr:
            return jsonify({
                'success': True,
                'name': self.name,
                'comment': self.comment,
            })
        return redirect(request.referrer)

    def send_mail(self, receivers=None):
        """Send mail when task created.

        :param receivers: Receivers of email.
        """
        subject = "[#%s %s] - %s" % (
            self.id, self.parent.name, self.name
        )

        if not receivers:
            receivers = [s.email for s in self.participants
                         if s.email]
        if self.created_by.email in receivers:
            receivers.remove(self.created_by.email)

        if not receivers:
            return

        message = render_email(
            from_email=CONFIG['smtp_from'],
            to=', '.join(receivers),
            subject=subject,
            text_template='project/emails/project_text_content.jinja',
            html_template='project/emails/project_html_content.jinja',
            task=self,
            updated_by=request.nereid_user.name
        )

        #Send mail.
        server = get_smtp_server()
        server.sendmail(CONFIG['smtp_from'], receivers,
            message.as_string())
        server.quit()

    @classmethod
    @login_required
    def unwatch(cls, task_id):
        """
        Remove the current user from the participants of the task

        :params task_id: task's id to unwatch.
        """
        task = cls.get_task(task_id)

        if request.nereid_user in task.participants:
            cls.write(
                [task], {
                    'participants': [('unlink', [request.nereid_user.id])]
                }
            )
        if request.is_xhr:
            return jsonify({'success': True})
        return redirect(request.referrer)

    @classmethod
    @login_required
    def watch(cls, task_id):
        """
        Add the current user from the participants of the task

        :params task_id: task's id to watch.
        """
        task = cls.get_task(task_id)

        if request.nereid_user not in task.participants:
            cls.write(
                [task], {
                    'participants': [('add', [request.nereid_user.id])]
                }
            )
        if request.is_xhr:
            return jsonify({'success': True})
        return redirect(request.referrer)

    @classmethod
    @login_required
    def permissions(cls, project_id):
        """
        Permissions for the project

        :params project_id: project's id to check permission
        """
        ProjectInvitation = Pool().get('project.work.invitation')

        project = cls.get_project(project_id)

        invitations = ProjectInvitation.search([
            ('project', '=', project.id),
            ('nereid_user', '=', False)
        ])
        return render_template(
            'project/permissions.jinja', project=project,
            invitations=invitations, active_type_name='permissions'
        )

    @classmethod
    @login_required
    def projects_list(cls, page=1):
        """
        Render a list of projects
        """
        projects = cls.search([
            ('party', '=', request.nereid_user.party),
        ])
        return render_template('project/projects.jinja', projects=projects)

    @classmethod
    @login_required
    def invite(cls, project_id):
        """Invite a user via email to the project

        :param project_id: ID of Project
        """
        NereidUser = Pool().get('nereid.user')
        ProjectInvitation = Pool().get('project.work.invitation')

        if not request.method == 'POST':
            return abort(404)

        project = cls.get_project(project_id)
        email = request.form['email']

        existing_user = NereidUser.search([
            ('email', '=', email),
            ('company', '=', request.nereid_website.company.id),
        ], limit=1)

        subject = '[%s] You have been invited to join the project' \
            % project.name
        if existing_user:
            # If participant already existed 
            if existing_user[0] in project.participants:
                flash("%s has been already added as a participant \
                    for the project" % existing_user[0].display_name)
                return redirect(request.referrer)

            email_message = render_email(
                text_template=
                    'project/emails/inform_addition_2_project_text.html',
                subject=subject, to=email, from_email=CONFIG['smtp_from'],
                project=project, user=existing_user[0]
            )
            cls.write(
                [project], {
                    'participants': [('add', [existing_user[0].id])]
                }
            )
            flash_message = "%s has been invited to the project" \
                % existing_user[0].display_name

        else:
            new_invite = ProjectInvitation.create({
                'email': email,
                'project': project.id,
            })
            email_message = render_email(
                text_template='project/emails/invite_2_project_text.html',
                subject=subject, to=email, from_email=CONFIG['smtp_from'],
                project=project, invitation=new_invite
            )
            flash_message = "%s has been invited to the project" % email

        server = get_smtp_server()
        server.sendmail(CONFIG['smtp_from'], [email],
            email_message.as_string())
        server.quit()

        if request.is_xhr:
            return jsonify({
                'success': True,
            })
        flash(flash_message)
        return redirect(request.referrer)

    @login_required
    def remove_participant(self, participant_id):
        """Remove the participant form project
        """
        # Check if user is among the project admins
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to remove participants." +
                " Contact your project admin for the same.")
            return redirect(request.referrer)

        if request.method == 'POST' and request.is_xhr:
            records_to_update_ids = [self.id]
            records_to_update_ids.extend([child.id for child in self.children])
            # If this participant is assigned to any task in this project,
            # that user cannot be removed as tryton's domain does not permit
            # this.
            # So removing assigned user from those tasks as well.
            # TODO: Find a better way to do it, this is memory intensive
            assigned_to_participant = self.search([
                ('id', 'in', records_to_update_ids),
                ('assigned_to', '=', participant_id)
            ])
            self.write(assigned_to_participant, {
                'assigned_to': False,
            })
            self.write(
                map(
                    lambda rec_id:
                        self.__class__(rec_id), records_to_update_ids
                ), {'participants': [('unlink', [participant_id])]}
            )

            return jsonify({
                'success': True,
            })

        flash("Could not remove participant! Try again.")
        return redirect(request.referrer)

    @classmethod
    @login_required
    def render_task_list(cls, project_id):
        """
        Renders a project's task list page
        """
        project = cls.get_project(project_id)
        state = request.args.get('state', None)
        page = request.args.get('page', 1, int)

        filter_domain = [
            ('type', '=', 'task'),
            ('parent', '=', project.id),
        ]

        query = request.args.get('q', None)
        if query:
            # This search is probably the suckiest search in the
            # history of mankind in terms of scalability and utility
            # TODO: Figure out something better
            filter_domain.append(('name', 'ilike', '%%%s%%' % query))

        tag = request.args.get('tag', None, int)
        if tag:
            filter_domain.append(('tags', '=', tag))

        user = request.args.get('user', None, int)
        if user:
            filter_domain.append(('assigned_to', '=', user))

        counts = {}
        counts['opened_tasks_count'] = cls.search(
            filter_domain + [('state', '=', 'opened')], count=True
        )
        counts['done_tasks_count'] = cls.search(
            filter_domain + [('state', '=', 'done')], count=True
        )
        counts['all_tasks_count'] = cls.search(
            filter_domain, count=True
        )

        if state and state in ('opened', 'done'):
            filter_domain.append(('state', '=', state))

        if request.is_xhr:
            tasks = cls.search(filter_domain)
            return jsonify({
                'items': map(lambda task: task.serialize(), tasks),
                'domain': filter_domain,
            })

        tasks = Pagination(cls, filter_domain, page, 10)
        return render_template(
            'project/project-task-list.jinja', project=project,
            active_type_name='render_task_list', counts=counts,
            state_filter=state, tasks=tasks
        )

    @classmethod
    @login_required
    def my_tasks(cls):
        """
        Renders all tasks of the user in all projects
        """
        state = request.args.get('state', None)

        filter_domain = [
            ('type', '=', 'task'),
            ('assigned_to', '=', request.nereid_user.id)
        ]
        query = request.args.get('q', None)
        if query:
            # This search is probably the suckiest search in the
            # history of mankind in terms of scalability and utility
            # TODO: Figure out something better
            filter_domain.append(('name', 'ilike', '%%%s%%' % query))

        tag = request.args.get('tag', None, int)
        if tag:
            filter_domain.append(('tags', '=', tag))

        counts = {}
        counts['opened_tasks_count'] = cls.search(
            filter_domain + [('state', '=', 'opened')], count=True
        )

        if state and state in ('opened', 'done'):
            filter_domain.append(('state', '=', state))

        tasks = cls.search(filter_domain, order=[('progress_state', 'ASC')])

        if request.is_xhr:
            return jsonify({
                'items': map(lambda task: task.serialize(), tasks),
                'domain': filter_domain,
            })

        # Group and return tasks for regular web viewing
        tasks_by_state = defaultdict(list)
        for task in tasks:
            tasks_by_state[task.progress_state].append(task)
        return render_template(
            'project/global-task-list.jinja',
            active_type_name='render_task_list', counts=counts,
            state_filter=state, tasks_by_state=tasks_by_state,
            states=PROGRESS_STATES
        )

    @classmethod
    @login_required
    def render_task(cls, task_id, project_id):
        """
        Renders the task in a project
        """
        task = cls.get_task(task_id)

        comments = sorted(
            task.history + task.timesheet_lines + task.attachments + \
                task.repo_commits, key=lambda x: x.create_date
        )

        hours={}
        for line in task.timesheet_lines:
            hours[line.employee] = hours.setdefault(line.employee, 0) + \
                    line.hours

        if request.is_xhr:
            response = cls.serialize(task)
            return jsonify(response)

        return render_template(
            'project/task.jinja', task=task, \
            active_type_name='render_task_list', project=task.parent,
            comments=comments, timesheet_summary=hours
        )

    @classmethod
    @login_required
    def render_files(cls, project_id):
        project = cls.get_project(project_id)
        other_attachments = chain.from_iterable(
            [list(task.attachments) for task in project.children if \
                task.attachments]
        )
        return render_template(
            'project/files.jinja', project=project,
            active_type_name='files', guess_type=guess_type,
            other_attachments=other_attachments
        )

    @classmethod
    def _get_expected_date_range(cls):
        """Return the start and end date based on the GET arguments.
        Also asjust for the full calendar asking for more information than
        it actually must show

        The last argument of the tuple returns a boolean to show if the
        weekly table/aggregation should be displayed
        """
        start = datetime.fromtimestamp(
            request.args.get('start', type=int)
        ).date()
        end = datetime.fromtimestamp(
            request.args.get('end', type=int)
        ).date()
        if (end - start).days < 20:
            # this is not a month call, just some smaller range
            return start, end
        # This is a data call for a month, but fullcalendar tries to
        # fill all the days in the first and last week from the prev
        # and next month. So just return start and end date of the month
        mid_date = start + relativedelta(days=((end - start).days / 2))
        ignore, last_day = calendar.monthrange(mid_date.year, mid_date.month)
        return (
            date(year=mid_date.year, month=mid_date.month, day=1),
            date(year=mid_date.year, month=mid_date.month, day=last_day),
        )

    @classmethod
    def get_week(cls, day):
        """
        Return the week for any given day
        """
        if (day >= 1) and (day <= 7):
            return '01-07'
        elif (day >= 8) and (day <= 14):
            return '08-14'
        elif (day >= 15) and (day <= 21):
            return '15-21'
        else:
            return '22-END'

    @classmethod
    def get_calendar_data(cls, domain=None):
        """
        Returns the calendar data

        :param domain: List of tuple to add to the domain expression
        """
        Timesheet = Pool().get('timesheet.line')

        start, end = cls._get_expected_date_range()

        if domain is None:
            domain = []
        domain += [
            ('date', '>=', start),
            ('date', '<=', end),
        ]

        if request.args.get('employee', None) and \
            request.nereid_user.has_permissions(['project.admin']):
            domain.append(
                ('employee', '=', request.args.get('employee', None, int))
            )
        lines = Timesheet.search(
            domain, order=[('date', 'asc'), ('employee', 'asc')]
        )

        hours_by_day_employee = defaultdict(lambda: defaultdict(float))
        hours_by_week_employee = defaultdict(lambda: defaultdict(float))

        for line in lines:
            hours_by_day_employee[line.date][line.employee] += line.hours
            hours_by_week_employee[cls.get_week(line.date.day)] \
                [line.employee] += line.hours

        day_totals = []
        color_map = {}
        colors = cycle([
            'grey', 'RoyalBlue', 'CornflowerBlue', 'DarkSeaGreen',
            'SeaGreen', 'Silver', 'MediumOrchid', 'Olive',
            'maroon', 'PaleTurquoise'
        ])
        day_totals_append = day_totals.append  # Performance speedup
        for date, employee_hours in hours_by_day_employee.iteritems():
            for employee, hours in employee_hours.iteritems():
                day_totals_append({
                    'id': '%s.%s' % (date, employee.id),
                    'title': '%s (%dh %dm)' % (
                        employee.name, hours, (hours * 60) % 60
                    ),
                    'start': date.isoformat(),
                    'color': color_map.setdefault(employee, colors.next()),
                })

        def get_task_from_work(work):
            '''
            Returns task from work

            :param work: Instance of work
            '''
            with Transaction().set_context(active_test=False):
                task, = cls.search([('work', '=', work.id)], limit=1)
                return task

        reverse_lines = lines[::-1]
        lines = [
            render_template(
                    'project/timesheet-line.jinja', line=line,
                    related_task=get_task_from_work(line.work)
                ) \
                for line in reverse_lines
        ]

        total_by_employee = defaultdict(float)
        for employee_hours in hours_by_week_employee.values():
            for employee, hours in employee_hours.iteritems():
                total_by_employee[employee] += hours

        work_week =  render_template(
            'project/work-week.jinja', data_by_week=hours_by_week_employee,
            total_by_employee=total_by_employee
        )
        return jsonify(day_totals=day_totals, lines=lines, work_week=work_week)

    @classmethod
    @login_required
    def get_7_day_performance(cls):
        """
        Returns the hours worked in the last 7 days.
        """
        Timesheet = Pool().get('timesheet.line')
        Date_ = Pool().get('ir.date')

        if not request.nereid_user.employee:
            return jsonify({})

        end_date = Date_.today()
        start_date = end_date - relativedelta(days=7)

        timesheet_lines = Timesheet.search([
            ('date', '>=', start_date),
            ('date', '<=', end_date),
            ('employee', '=', request.nereid_user.employee.id),
        ])

        hours_by_day = {}

        for line in timesheet_lines:
            hours_by_day[line.date] = \
                hours_by_day.setdefault(line.date, 0.0) + line.hours

        days = hours_by_day.keys()
        days.sort()

        return jsonify({
            'categories': map(lambda d:d.strftime('%d-%b'), days),
            'series': ['%.2f' % hours_by_day[day] for day in days]
        })

    @classmethod
    def get_comparison_data(cls):
        """
        Compare the performance of people
        """
        Date = Pool().get('ir.date')
        Employee = Pool().get('company.employee')

        employee_ids = request.args.getlist('employee', type=int)
        if not employee_ids:
            employees = Employee.search([])
            employee_ids = map(lambda emp: emp.id, employees)
        else:
            employees = Employee.browse(employee_ids)

        employees = dict(zip(employee_ids, employees))

        end_date = Date.today()
        if request.args.get('end_date'):
            end_date = parse_date(
                    request.args['end_date'],
                    locale='en_IN',
                    #locale=Transaction().context.get('language')
            )
        start_date = end_date - relativedelta(months=1)
        if request.args.get('start_date'):
            start_date = parse_date(
                    request.args['start_date'],
                    locale='en_IN',
                    #locale=Transaction().context.get('language')
            )

        if start_date > end_date:
            flash('Invalid date Range')
            return redirect(request.referrer)

        Transaction().cursor.execute(
            'SELECT * FROM timesheet_by_employee_by_day '
            'WHERE "date" >= %s '
            'AND "date" <= %s '
            'AND "employee" in %s '
            'ORDER BY "date"',
            (start_date, end_date, tuple(employee_ids))
        )
        raw_data = Transaction().cursor.fetchall()
        categories = map(
            lambda d: start_date + relativedelta(days=d),
            range(0, (end_date - start_date).days + 1)
        )
        hours_by_date_by_employee = {}
        for employee_id, line_date, hours in raw_data:
            hours_by_date_by_employee.setdefault(line_date, {}) \
                [employee_id] = hours

        series = []
        for employee_id in employee_ids:
            employee = employees.get(employee_id)
            series.append({
                'name': employee and employee.name or 'Ghost',
                'type': 'column',
                'data': map(
                    lambda d: \
                        hours_by_date_by_employee.get(d, {}) \
                        .get(employee_id, 0), categories
                )
            })

        additional = [{
            'type': 'pie',
            'name': 'Total Hours',
            'data': map(
                lambda d: {'name': d['name'], 'y': sum(d['data'])}, series
            ),
            'center': [40, 40],
            'size': 100,
            'showInLegend': False,
            'dataLabels': {
                'enabled': False
            }
        }]
        additional.extend([{
            'type': 'line',
            'name': '{0} Avg'.format(serie['name']),
            'data': [sum(serie['data']) / len(serie['data'])] * len(categories),
        } for serie in series])

        return jsonify(
            categories=map(lambda d: d.strftime('%d-%b'), categories),
            series=series + additional
        )

    @classmethod
    def get_gantt_data(cls):
        """
        Get gantt data for the last 1 month.
        """
        Date = Pool().get('ir.date')
        Employee = Pool().get('company.employee')

        employees = Employee.search([])
        employee_ids = map(lambda emp: emp.id, employees)
        employees = dict(zip(employee_ids, employees))

        today = Date.today()
        start_date = today - relativedelta(months=2)

        Transaction().cursor.execute(
            'SELECT * FROM timesheet_by_employee_by_day '
            'WHERE "date" >= \'%s\'' % (start_date, )
        )
        raw_data = Transaction().cursor.fetchall()
        employee_wise_data = {}
        get_class = lambda h: (h < 4) and 'ganttRed' or \
                                (h < 6) and 'ganttOrange' or 'ganttGreen'
        for employee_id, line_date, hours in raw_data:
            value = {
                'from': line_date - relativedelta(days=1), # Gantt has a bug of 1 day off
                'to': line_date - relativedelta(days=1),
                'label': '%.1f' % hours,
                'customClass': get_class(hours)
            }
            employee_wise_data.setdefault(employee_id, []).append(value)

        gantt_data = []
        gantt_data_append = gantt_data.append
        for employee_id, values in employee_wise_data.iteritems():
            employee = employees.get(employee_id)
            gantt_data_append({
                'name': employee and employee.name or 'Ghost',
		'desc': '',
                'values': values,
            })
        gantt_data = sorted(gantt_data, key=lambda item: item['name'].lower())
        date_handler = lambda o: '/Date(%d)/' % (time.mktime(o.timetuple()) * 1000) \
            if hasattr(o, 'timetuple') else o
        return json.dumps(gantt_data, default=date_handler)

    @classmethod
    @login_required
    @permissions_required(['project.admin'])
    def compare_performance(cls):
        """
        Compare the performance of people
        """
        Employee = Pool().get('company.employee')
        Date = Pool().get('ir.date')

        if request.is_xhr:
            return cls.get_comparison_data()
        employees = Employee.search([])
        today = Date.today()
        return render_template(
            'project/compare-performance.jinja', employees=employees,
            start_date=today-relativedelta(days=7),
            end_date=today
        )

    @classmethod
    @login_required
    @permissions_required(['project.admin'])
    def render_global_gantt(cls):
        """
        Renders a global gantt
        """
        Employee = Pool().get('company.employee')

        if request.is_xhr:
            return cls.get_gantt_data()
        employees = Employee.search([])
        return render_template(
            'project/global-gantt.jinja', employees=employees
        )

    @classmethod
    @login_required
    @permissions_required(['project.admin'])
    def render_global_timesheet(cls):
        '''
        Returns rendered timesheet template.
        '''
        Employee = Pool().get('company.employee')

        if request.is_xhr:
            return cls.get_calendar_data()
        employees = Employee.search([])
        return render_template(
            'project/global-timesheet.jinja', employees=employees
        )

    @classmethod
    @login_required
    @permissions_required(['project.admin'])
    def render_tasks_by_employee(cls):
        '''
        Returns rendered task, for employee.
        '''
        open_tasks = cls.search([
            ('state', '=', 'opened'),
            ('assigned_to.employee', '!=', None),
            ], order=[('assigned_to', 'ASC')]
        )
        tasks_by_employee_by_state = defaultdict(lambda: defaultdict(list))
        for task in open_tasks:
            tasks_by_employee_by_state[task.assigned_to][
                task.progress_state
            ].append(task)
        employees = tasks_by_employee_by_state.keys()
        employees.sort()
        return render_template(
            'project/tasks-by-employee.jinja',
            tasks_by_employee_by_state=tasks_by_employee_by_state,
            employees=employees,
            states=PROGRESS_STATES,
        )

    @classmethod
    @login_required
    def render_timesheet(cls, project_id):
        '''
        Returns rendered timesheet template.
        '''
        project = cls.get_project(project_id)
        employees = [
            p.employee for p in project.all_participants \
                if p.employee
        ]
        if request.is_xhr:
            return cls.get_calendar_data(
                [('work.parent', 'child_of', [project.work.id])]
            )
        return render_template(
            'project/timesheet.jinja', project=project,
            active_type_name="timesheet", employees=employees
        )

    @classmethod
    @login_required
    def render_plan(cls, project_id):
        """
        Render the plan of the project
        """
        project = cls.get_project(project_id)
        if request.is_xhr:
            # XHTTP Request probably from the calendar widget, answer that
            # with json
            start = datetime.fromtimestamp(
                request.args.get('start', type=int)
            )
            end = datetime.fromtimestamp(
                request.args.get('end', type=int)
            )
            # TODO: These times are local times of the user, convert them to
            # UTC (Server time) before using them for comparison
            tasks = cls.search(['AND',
                ('type', '=', 'task'),
                ('parent', '=', project.id),
                ['OR',
                    [
                       ('constraint_start_time', '>=', start),
                    ],
                    [
                        ('constraint_finish_time', '<=', end),
                    ],
                    [
                       ('actual_start_time', '>=', start),
                    ],
                    [
                        ('actual_finish_time', '<=', end),
                    ],
                ]
            ])
            event_type = request.args['event_type']
            assert event_type in ('constraint', 'actual')

            def to_event(task, type="constraint"):
                event = {
                    'id': task.id,
                    'title': task.name,
                    'url': url_for(
                        'project.work.render_task',
                        project_id=task.parent.id, task_id=task.id),
                }
                event["start"] = getattr(
                    task, '%s_start_time' % type
                ).isoformat()
                if getattr(task, '%s_finish_time' % type):
                    event['end'] = getattr(
                        task, '%s_finish_time' % type
                    ).isoformat()
                return event

            return jsonify(
                result = [
                    # Send all events where there is a start time
                    to_event(task, event_type) for task in tasks \
                        if getattr(task, '%s_start_time' % event_type)
                ]
            )

        return render_template(
            'project/plan.jinja', project=project,
            active_type_name='plan'
        )

    @classmethod
    @login_required
    def download_file(cls, attachment_id):
        """
        Returns the file for download. The ownership of the task or the
        project is checked automatically.
        """
        Attachment = Pool().get('ir.attachment')

        work = None
        if request.args.get('project', None):
            work = cls.get_project(request.args.get('project', type=int))
        if request.args.get('task', None):
            work = cls.get_task(request.args.get('task', type=int))

        if not work:
            # Neither task, nor the project is specified
            raise abort(404)

        attachment, = Attachment.search([
            ('id', '=', attachment_id),
            ('resource', '=', '%s,%d' % (cls.__name__, work.id))
        ], limit=1)

        if attachment.type == 'link':
            return redirect(attachment.link)

        if not attachment:
            raise abort(404)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(attachment.data)

        return send_file(
            f.name, attachment_filename=attachment.name, as_attachment=True
        )

    @classmethod
    @login_required
    def upload_file(cls):
        """
        Upload the file to a project or task with owner/uploader
        as the current user
        """
        Attachment = Pool().get('ir.attachment')

        work = None
        if request.form.get('project', None):
            work = cls.get_project(request.form.get('project', type=int))
        if request.form.get('task', None):
            work = cls.get_task(request.form.get('task', type=int))

        if not work:
            # Neither task, nor the project is specified
            raise abort(404)

        attached_file =  request.files["file"]
        resource = '%s,%d' % (cls.__name__, work.id)

        if Attachment.search([
            ('name', '=', attached_file.filename),
            ('resource', '=', resource)
        ]):
            flash(
                'File already exists with same name, please choose another ' +
                'file or rename this file to upload !!'
            )
            return redirect(request.referrer)

        data = {
            'resource': resource,
            'description': request.form.get('description', '')
        }

        if request.form.get('file_type') == 'link':
            link = request.form.get('url')
            data.update({
                'link': link,
                'name': link.split('/')[-1],
                'type': 'link'
            })
        else:
            data.update({
                'data': attached_file.stream.read(),
                'name': attached_file.filename,
                'type': 'data'
            })

        Attachment.create(data)

        if request.is_xhr:
            return jsonify({
                'success': True
            })

        flash("Attachment added to %s" % work.name)
        return redirect(request.referrer)

    @classmethod
    @login_required
    def update_task(cls, task_id):
        """
        Accepts a POST request against a task_id and updates the ticket

        :param task_id: The ID of the task which needs to be updated
        """
        History = Pool().get('project.work.history')
        TimesheetLine = Pool().get('timesheet.line')

        task = cls.get_task(task_id)

        history_data = {
            'project': task.id,
            'updated_by': request.nereid_user.id,
            'comment': request.form['comment']
        }

        updatable_attrs = ['state', 'progress_state']
        new_participant_ids = []
        current_participant_ids = [p.id for p in task.participants]
        post_attrs = [request.form.get(attr, None) for attr in updatable_attrs]
        if any(post_attrs):
            # Combined update of task and history since there is some value
            # posted in addition to the comment
            task_changes = {}
            for attr in updatable_attrs:
                if getattr(task, attr) != request.form.get(attr, None):
                    task_changes[attr] = request.form[attr]

            new_assignee_id = request.form.get('assigned_to', None, int)
            if not new_assignee_id == None:
                if (new_assignee_id and \
                        (not task.assigned_to or \
                            new_assignee_id != task.assigned_to.id)) \
                        or (request.form.get('assigned_to', None) == ""): # Clear the user
                    history_data['previous_assigned_to'] = \
                        task.assigned_to and task.assigned_to.id or None
                    history_data['new_assigned_to'] = new_assignee_id
                    task_changes['assigned_to'] = new_assignee_id
                    if new_assignee_id and new_assignee_id not in \
                        current_participant_ids:
                        new_participant_ids.append(new_assignee_id)

            if task_changes:
                # Only write change if anything has really changed
                cls.write([task], task_changes)
                comment = task.history[-1]
                History.write([comment], history_data)
            else:
                # just create comment since nothing really changed since this
                # update. This is to cover to cover cases where two users who
                # havent refreshed the web page close the ticket
                comment = History.create(history_data)
        else:
            # Just comment, no update to task
            comment = History.create(history_data)


        if request.nereid_user.id not in current_participant_ids:
            # Add the user to the participants if not already in the list
            new_participant_ids.append(request.nereid_user.id)

        for nereid_user in request.form.getlist('notify[]', int):
            # Notify more people if there are people
            # who havent been added as participants
            if nereid_user not in current_participant_ids:
                new_participant_ids.append(nereid_user)

        if new_participant_ids:
            cls.write(
                [task], {'participants': [('add', new_participant_ids)]}
            )

        hours = request.form.get('hours', None, type=float)
        if hours and request.nereid_user.employee:
            TimesheetLine.create({
                'employee': request.nereid_user.employee.id,
                'hours': hours,
                'work': task.work.id
            })

        # Send the email since all thats required is done
        comment.send_mail()

        if request.is_xhr:
            html = render_template(
                'project/comment.jinja', comment=comment)
            return jsonify({
                'success': True,
                'html': html,
                'state': task.state,
                'progress_state': task.progress_state,
            })
        return redirect(request.referrer)

    @classmethod
    @login_required
    def add_tag(cls, task_id, tag_id):
        """
        Assigns the provided to this task

        :param task_id: ID of task
        :param tag_id: ID of tag
        """
        task = cls.get_task(task_id)

        cls.write(
            [task], {'tags': [('add', [tag_id])]}
        )

        if request.method == 'POST':
            flash('Tag added to task %s' % task.name)
            return redirect(request.referrer)

        flash("Tag cannot be added")
        return redirect(request.referrer)

    @classmethod
    @login_required
    def remove_tag(cls, task_id, tag_id):
        """
        Assigns the provided to this task

        :param task_id: ID of task
        :param tag_id: ID of tag
        """
        task = cls.get_task(task_id)

        cls.write(
            [task], {'tags': [('unlink', [tag_id])]}
        )

        if request.method == 'POST':
            flash('Tag removed from task %s' % task.name)
            return redirect(request.referrer)

        flash("Tag cannot be removed")
        return redirect(request.referrer)

    @classmethod
    def write(cls, projects, values):
        """
        Update write to historize everytime an update is made

        :param ids: ids of the projects
        :param values: A dictionary
        """
        WorkHistory = Pool().get('project.work.history')

        for project in projects:
            WorkHistory.create_history_line(project, values)

        return super(Project, cls).write(projects, values)

    @classmethod
    @login_required
    def mark_time(cls, task_id):
        """
        Marks the time against the employee for the task

        :param task_id: ID of task
        """
        TimesheetLine = Pool().get('timesheet.line')

        if not request.nereid_user.employee:
            flash("Only employees can mark time on tasks!")
            return redirect(request.referrer)

        task = cls.get_task(task_id)

        with Transaction().set_user(0):
            TimesheetLine.create({
                'employee': request.nereid_user.employee.id,
                'hours': request.form['hours'],
                'work': task.work.id
            })

        flash("Time has been marked on task %s" % task.name)
        return redirect(request.referrer)

    @classmethod
    @login_required
    def assign_task(cls, task_id):
        """
        Assign task to a user

        :param task_id: Id of Task
        """
        NereidUser = Pool().get('nereid.user')

        task = cls.get_task(task_id)

        new_assignee = NereidUser(int(request.form['user']))

        if task.assigned_to == new_assignee:
            flash("Task already assigned to %s" % new_assignee.name)
            return redirect(request.referrer)
        if task.parent.can_write(new_assignee):
            cls.write([task], {
                'assigned_to': new_assignee.id,
                'participants': [('add', [new_assignee.id])]
            })
            task.history[-1].send_mail()
            if request.is_xhr:
                return jsonify({
                    'success': True,
                })

            flash("Task assigned to %s" % new_assignee.name)
            return redirect(request.referrer)
        flash("Only employees can be assigned to tasks.")
        return redirect(request.referrer)

    @classmethod
    @login_required
    def clear_assigned_user(cls, task_id):
        """Clear the assigned user from the task

        :param task_id: Id of Task
        """
        task = cls.get_task(task_id)

        cls.write([task], {
            'assigned_to': False
        })

        if request.is_xhr:
            return jsonify({
                'success': True,
            })

        flash("Removed the assigned user from task")
        return redirect(request.referrer)

    @classmethod
    @login_required
    def change_constraint_dates(cls, task_id):
        """
        Change the constraint dates
        """
        task = cls.get_task(task_id)

        data = {
            'constraint_start_time': False,
            'constraint_finish_time': False
        }

        constraint_start = request.form.get('constraint_start_time', None)
        constraint_finish = request.form.get('constraint_finish_time', None)

        if constraint_start:
            data['constraint_start_time'] = datetime.strptime(
                constraint_start, '%m/%d/%Y')
        if constraint_finish:
            data['constraint_finish_time'] = datetime.strptime(
                constraint_finish, '%m/%d/%Y')

        cls.write([task], data)

        if request.is_xhr:
            return jsonify({
                'success': True,
            })

        flash("The constraint dates have been changed for this task.")
        return redirect(request.referrer)

    @classmethod
    @login_required
    def delete_task(cls, task_id):
        """Delete the task from project
        """
        # Check if user is among the project admins
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to delete tags. \
                Contact your project admin for the same.")
            return redirect(request.referrer)

        task = cls.get_task(task_id)

        cls.write([task], {'active': False})

        if request.is_xhr:
            return jsonify({
                'success': True,
            })

        flash("The task has been deleted")
        return redirect(
            url_for('project.work.render_project', project_id=task.parent.id)
        )

    @login_required
    def change_estimated_hours(self):
        """Change estimated hours.

        :param task_id: ID of the task.
        """
        if not request.nereid_user.employee:
            flash("Sorry! You are not allowed to change estimate hours.")
            return redirect(request.referrer)

        estimated_hours = request.form.get(
            'new_estimated_hours', None, type=float
        )

        if estimated_hours:
            self.write([self], {
                'effort': estimated_hours,
                }
            )

        flash("The estimated hours have been changed for this task.")
        return redirect(request.referrer)


class Tag(ModelSQL, ModelView):
    "Tags"
    __name__ = "project.work.tag"

    name = fields.Char('Name', required=True)
    color = fields.Char('Color Code', required=True)
    project = fields.Many2One(
        'project.work', 'Project', required=True,
        domain=[('type', '=', 'project')], ondelete='CASCADE',
    )

    @classmethod
    def __setup__(cls):
        super(Tag, cls).__setup__()
        cls._sql_constraints += [
           ('unique_name_project', 'UNIQUE(name, project)', 'Duplicate Tag')
        ]

    @staticmethod
    def default_color():
        '''
        Default for color
        '''
        return "#999"

    @classmethod
    @login_required
    def create_tag(cls, project_id):
        """
        Create a new tag for the specific project

        :params project_id: Project id for which need to be created
        """
        Project = Pool().get('project.work')

        project = Project.get_project(project_id)

        # Check if user is among the project admins
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to create new tags." +
                " Contact your project admin for the same.")
            return redirect(request.referrer)

        if request.method == 'POST':
            cls.create({
                'name': request.form['name'],
                'color': request.form['color'],
                'project': project.id
            })

            flash("Successfully created tag")
            return redirect(request.referrer)

        flash("Could not create tag. Try Again")
        return redirect(request.referrer)

    @login_required
    def delete_tag(self):
        """
        Delete the tag from project
        """
        # Check if user is among the project admins
        if not request.nereid_user.is_project_admin():
            flash("Sorry! You are not allowed to delete tags." +
                " Contact your project admin for the same.")
            return redirect(request.referrer)

        if request.method == 'POST' and request.is_xhr:
            self.delete([self])

            return jsonify({
                'success': True,
            })

        flash("Could not delete tag! Try again.")
        return redirect(request.referrer)


class TaskTags(ModelSQL):
    'Task Tags'
    __name__ = 'project.work-project.work.tag'

    task = fields.Many2One(
        'project.work', 'Project',
        ondelete='CASCADE', select=1, required=True,
        domain=[('type', '=', 'task')]
    )

    tag = fields.Many2One(
       'project.work.tag', 'Tag', select=1, required=True, ondelete='CASCADE',
    )

    @classmethod
    def __register__(cls, module_name):
        '''
        Register class and update table name to new.
        '''
        cursor = Transaction().cursor
        table  = TableHandler(cursor, cls, module_name)
        super(TaskTags, cls).__register__(module_name)

        # Migration
        if table.table_exist(cursor, 'project_work_tag_rel'):
            table.table_rename(
                cursor, 'project_work_tag_rel', 'project_work-project_work_tag'
            )


class ProjectHistory(ModelSQL, ModelView):
    'Project Work History'
    __name__ = 'project.work.history'

    date = fields.DateTime('Change Date')  # date is python built
    create_uid = fields.Many2One('res.user', 'Create User')

    #: The reverse many to one for history field to work
    project = fields.Many2One('project.work', 'Project Work')

    # Nereid user who made this update
    updated_by = fields.Many2One('nereid.user', 'Updated By')


    # States
    previous_state = fields.Selection([
        ('opened', 'Opened'),
        ('done', 'Done'),
        ], 'Prev. State', select=True)
    new_state = fields.Selection([
        ('opened', 'Opened'),
        ('done', 'Done'),
        ], 'New State', select=True)
    previous_progress_state = fields.Selection(
        PROGRESS_STATES, 'Prev. Progress State', select=True
    )
    new_progress_state = fields.Selection(
        PROGRESS_STATES, 'New Progress State', select=True
    )

    # Comment
    comment = fields.Text('Comment')

    # Name
    previous_name = fields.Char('Prev. Name')
    new_name = fields.Char('New Name')

    # Assigned to
    previous_assigned_to = fields.Many2One('nereid.user', 'Prev. Assignee')
    new_assigned_to = fields.Many2One('nereid.user', 'New Assignee')

    # other fields
    previous_constraint_start_time = fields.DateTime("Constraint Start Time")
    new_constraint_start_time = fields.DateTime("Next Constraint Start Time")

    previous_constraint_finish_time = fields.DateTime("Constraint Finish Time")
    new_constraint_finish_time = fields.DateTime("Constraint  Finish Time")

    @staticmethod
    def default_date():
        '''
        Return current datetime in utcformat as default for date.
        '''
        return datetime.utcnow()

    @classmethod
    def create_history_line(cls, project, changed_values):
        """
        Creates a history line from the changed values of a project.work
        """
        if changed_values:
            data = {}

            # TODO: Also create a line when assigned user is cleared from task
            for field in ('assigned_to', 'state', 'progress_state',
                    'constraint_start_time', 'constraint_finish_time'):
                if field not in changed_values or not changed_values[field]:
                    continue
                data['previous_%s' % field] = getattr(project, field)
                data['new_%s' % field] = changed_values[field]

            if data:
                if has_request_context():
                    data['updated_by'] = request.nereid_user.id
                else:
                    # TODO: try to find the nereid user from the employee
                    # if an employee made the update
                    pass
                data['project'] = project.id
                return cls.create(data)

    @login_required
    def update_comment(self, task_id):
        """
        Update a specific comment.
        """
        Project = Pool().get('project.work')

        # allow modification only if the user is an admin or the author of
        # this ticket
        task = Project(task_id)
        assert task.type == "task"
        assert self.project.id == task.id

        # Allow only admins and author of this comment to edit it
        if request.nereid_user.is_project_admin() or \
                self.updated_by == request.nereid_user:
            self.write([self], {'comment': request.form['comment']})
        else:
            abort(403)

        if request.is_xhr:
            html = render_template('project/comment.jinja', comment=self)
            return jsonify({
                'success': True,
                'html': html,
                'state': task.state,
            })
        return redirect(request.referrer)

    def send_mail(self):
        """
        Send mail to all participants whenever there is any update on
        project.

        """
        # Get the previous updates than the latest one.
        last_history = self.search([
            ('id', '<', self.id),
            ('project.id', '=', self.project.id)
        ], order=[('create_date', 'DESC')])

        # Prepare the content of email.
        subject = "[#%s %s] - %s" % (
            self.project.id, self.project.parent.name,
            self.project.work.name,
        )

        receivers = map(
            lambda user: user.email,
            filter(lambda s: s.email, self.project.participants)
        )
        if self.updated_by.email in receivers:
            receivers.remove(self.updated_by.email)

        if not receivers:
            return

        message = render_email(
            from_email=CONFIG['smtp_from'],
            to=', '.join(receivers),
            subject=subject,
            text_template='project/emails/text_content.jinja',
            html_template='project/emails/html_content.jinja',
            history=self,
            last_history=last_history
        )

        #message.add_header('reply-to', request.nereid_user.email)

        # Send mail.
        server = get_smtp_server()
        server.sendmail(CONFIG['smtp_from'], receivers,
            message.as_string())
        server.quit()


class ProjectWorkCommit(ModelSQL, ModelView):
    "Repository commits"
    __name__ = 'project.work.commit'
    _rec_name = 'commit_message'

    commit_timestamp = fields.DateTime('Commit Timestamp')
    project = fields.Many2One(
        'project.work', 'Project', required=True, select=True
    )
    nereid_user = fields.Many2One(
        'nereid.user', 'User', required=True, select=True
    )
    repository = fields.Char('Repository Name', required=True, select=True)
    repository_url = fields.Char('Repository URL', required=True)
    commit_message = fields.Char('Commit Message', required=True)
    commit_url = fields.Char('Commit URL', required=True)
    commit_id = fields.Char('Commit Id', required=True)

    @classmethod
    def commit_github_hook_handler(cls):
        """
        Handle post commit posts from GitHub
        See https://help.github.com/articles/post-receive-hooks
        """
        NereidUser = Pool().get('nereid.user')

        if request.method == "POST":
            payload = json.loads(request.form['payload'])
            for commit in payload['commits']:
                nereid_users = NereidUser.search([
                    ('email', '=', commit['author']['email'])
                ])
                if not nereid_users:
                    continue

                projects = set([
                    int(x) for x in re.findall(
                        r'#(\d+)', commit['message']
                    )
                ])
                pull_requests = set([
                    int(x) for x in re.findall(
                        r'pull request #(\d+)', commit['message']
                    )
                ])
                for project in projects - pull_requests:
                    local_commit_time = dateutil.parser.parse(
                        commit['timestamp']
                    )
                    commit_timestamp = local_commit_time.astimezone(
                        dateutil.tz.tzutc()
                    )
                    cls.create({
                        'commit_timestamp': commit_timestamp,
                        'project': project,
                        'nereid_user': nereid_users[0].id,
                        'repository': payload['repository']['name'],
                        'repository_url': payload['repository']['url'],
                        'commit_message': commit['message'],
                        'commit_url': commit['url'],
                        'commit_id': commit['id']
                    })
        return 'OK'

    @classmethod
    def commit_bitbucket_hook_handler(cls):
        """
        Handle post commit posts from bitbucket
        See https://confluence.atlassian.com/display/BITBUCKET/POST+Service+Management
        """
        NereidUser = Pool().get('nereid.user')

        if request.method == "POST":
            payload = json.loads(request.form['payload'])
            for commit in payload['commits']:
                nereid_users = NereidUser.search([
                    ('email', '=', parseaddr(commit['raw_author'])[1])
                ])
                if not nereid_users:
                    continue

                projects = set([
                    int(x) for x in re.findall(
                        r'#(\d+)', commit['message']
                    )
                ])
                pull_requests = set([
                    int(x) for x in re.findall(
                        r'pull request #(\d+)', commit['message']
                )])
                for project in projects - pull_requests:
                    local_commit_time = dateutil.parser.parse(
                        commit['utctimestamp']
                    )
                    commit_timestamp = local_commit_time.astimezone(
                        dateutil.tz.tzutc()
                    )
                    cls.create({
                        'commit_timestamp': commit_timestamp,
                        'project': project,
                        'nereid_user': nereid_users[0].id,
                        'repository': payload['repository']['name'],
                        'repository_url': payload['canon_url'] + \
                            payload['repository']['absolute_url'],
                        'commit_message': commit['message'],
                        'commit_url': payload['canon_url'] + \
                        payload['repository']['absolute_url'] + \
                            "changeset/" + commit['raw_node'],
                            'commit_id': commit['raw_node']
                    })
        return 'OK'


@registration.connect
def invitation_new_user_handler(nereid_user_id):
    """When the invite is sent to a new user, he is sent an invitation key
    with the url which guides the user to registration page

        This method checks if the invitation key is present in the url
        If yes, search for the invitation with this key, attache the user
            to the invitation and project to the user
        If not, perform normal operation
    """
    try:
        Invitation = Pool().get('project.work.invitation')
        Project = Pool().get('project.work')
        NereidUser = Pool().get('nereid.user')

    except KeyError:
        # Just return silently. This KeyError is cause if the module is not
        # installed for a specific database but exists in the python path
        # and is loaded by the tryton module loader
        warnings.warn(
            "nereid-project module installed but not in database",
            DeprecationWarning, stacklevel=2
        )
        return

    invitation_code = request.args.get('invitation_code')
    if not invitation_code:
        return
    invitation, = Invitation.search([
        ('invitation_code', '=', invitation_code)
    ], limit=1)

    if not invitation:
        return

    Invitation.write([invitation], {
        'nereid_user': nereid_user_id,
        'invitation_code': None
    })

    nereid_user = NereidUser(nereid_user_id)

    subject = '[%s] %s Accepted the invitation to join the project' \
        % (invitation.project.name, nereid_user.display_name)

    receivers = [
        p.email for p in invitation.project.company.project_admins if p.email
    ]

    email_message = render_email(
        text_template='project/emails/invite_2_project_accepted_text.html',
        subject=subject, to=', '.join(receivers),
        from_email=CONFIG['smtp_from'], invitation=invitation
    )
    server = get_smtp_server()
    server.sendmail(CONFIG['smtp_from'], receivers, email_message.as_string())
    server.quit()

    Project.write(
        [invitation.project], {
            'participants': [('add', [nereid_user_id])]
        }
    )
