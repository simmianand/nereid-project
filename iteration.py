# -*- coding: utf-8 -*-
"""
    Iteration Model

    :copyright: (c) 2013 by Openlabs Technologies & Consulting (P) Limited
    :license: BSD, see LICENSE for more details.
"""
from nereid import (login_required, url_for, abort, request, flash, redirect,
    render_template, jsonify)
from trytond.model import ModelSQL, ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction
from nereid.contrib.pagination import Pagination
from datetime import datetime, date
from trytond.pyson import Eval
from nereid.ctx import has_request_context

DATE_FORMAT = '%m/%d/%Y'

__all__ = ['ProjectIteration', 'Work']

__metaclass__ = PoolMeta


class ProjectIteration(ModelSQL, ModelView):
    'Project Iteration'
    __name__ = 'project.iteration'

    name = fields.Char('Name', required=True)
    start_date = fields.Date('Start Date', required=True, select=True)
    end_date = fields.Date('End Date', required=True, select=True)
    project = fields.Many2One(
        'project.work', 'Project', required=True,
        domain=[('type', '=', 'project')], depends=['type'],
        ondelete='CASCADE',
    )
    items = fields.One2Many(
        'project.work', 'iteration', 'Items',
        domain=[
            ('type', '!=', 'project'),
            ('parent', '=', Eval('project'))
        ],
        add_remove=[
            ('type', '!=', 'project'),
            ('parent', '=', Eval('project'))
        ],
        depends=['project'],
    )
    url = fields.Function(fields.Char('URL'), 'get_url')

    def get_url(self, name):
        """
        Return the url if within an active request context or return None

        :param name: name of field
        """
        if has_request_context():
            return url_for(
                'project.iteration.render', project_id=self.project, active_id=self.id
            )
        else:
            return None

    @classmethod
    def __setup__(cls):
        super(ProjectIteration, cls).__setup__()
        cls._constraints += [
            ('check_dates', 'wrong_dates'),
        ]
        cls._order.insert(0, ('start_date', 'ASC'))
        cls._error_messages.update({
            'wrong_dates': 'End Date should be greater than Start Date or'
                'Iteration period is overlaping',
        })

    def check_dates(self):
        cursor = Transaction().cursor
        if self.start_date >= self.end_date:
            return False
        cursor.execute('SELECT id ' \
                    'FROM ' + self._table + ' ' \
                    'WHERE ((start_date <= %s AND end_date >= %s) ' \
                            'OR (start_date <= %s AND end_date >= %s) ' \
                            'OR (start_date >= %s AND end_date <= %s)) ' \
                        'AND project = %s ' \
                        'AND id != %s',
                    (self.start_date, self.start_date,
                        self.end_date, self.end_date,
                        self.start_date, self.end_date,
                        self.project.id, self.id))
        if cursor.fetchone():
            return False
        return True

    @classmethod
    @login_required
    def render_list(cls, project_id):
        """
        GET: Renders list of iterations.
        POST: Create new iteration.
        """
        Project = Pool().get('project.work')

        project = Project.get_project(project_id)

        if request.method == 'GET':
            pass
        if request.method == 'POST':
            iteration = cls.create({
                'name': request.form['name'],
                'start_date': datetime.strptime(request.values
                    ['start_date'], DATE_FORMAT).date(),
                'end_date': datetime.strptime(request.values
                    ['end_date'], DATE_FORMAT).date(),
                'project': project.id
            })
            return jsonify({
                'success': True,
                'url': iteration.url,
            })


    @login_required
    def render(self, project_id):
        '''
        GET: Renders an Iteration.
        POST: Add item to Iteration.
        PUT: Update Iteration.
        DELETE: Deletes an Iteration.
        '''
        Project = Pool().get('project.work')

        project = Project.get_project(project_id)

        if request.method == 'GET':
            return jsonify(self._json(self.browse(self.id)))
        if request.method == 'POST':
            pass
        if request.method == 'PUT':
            iteration = self.search([
                ('id', '=', self.id),
                ('project', '='. project.id),
            ])
            if not iteration:
                abort(403)

        if request.method == 'DELETE':
            self.delete([self])

            return jsonify({
                'success': True,
            })


class Work:
    """
    Project Work
    """
    __name__ = 'project.work'

    iteration = fields.Many2One(
        'project.iteration', 'Iteration',
        ondelete='CASCADE', select=True
    )
    type = fields.Selection([
            ('project', 'Project'),
            ('task', 'Task'),
            ('user_story', 'Story'),
            ],
        'Type', required=True, select=True,
    )
    #url = fields.Function(fields.Char('URL'), 'get_url')

    @classmethod
    def __setup__(cls):
        super(Work, cls).__setup__()

    @classmethod
    @login_required
    def render_iteration(cls, iteration_id, project_id):
        """
        Renders iteration in a project
        """
        Iteration = Pool().get('project.iteration')

        project =cls.get_project(project_id)

        iteration, = Iteration.search([
                ('id', '=', iteration_id),
                ('project.id', '=', project_id)
            ], limit=1)

        #iteration = Iteration.create({
                #'name': request.form['name'],
                #'start_date': datetime.strptime(request.values
                    #['start_date'], DATE_FORMAT).date(),
                #'end_date': datetime.strptime(request.values
                    #['end_date'], DATE_FORMAT).date(),
                #'project': project.id
                #})
        #if request.method == "POST":
            #iteration = Iteration.search([
                #('id', '=', iteration_id),
                #('project.id', '=', project_id)
            #], limit=1)

            #iteration, = Iteration.search([
                #('id', '=', iteration_id),
                #('project', '=', project_id)
            #])

        if not iteration:
            raise abort(404)

            #if not iteration.can_read(request.nereid_user):
                #raise abort(404)

        #if request.method == "POST":
            #iteration, = cls.search([
                #('id', '=', iteration_id),
                #('project', '=', project_id)
            #])

            #if not iteration:
                #raise abort(404)

            #if not iteration.can_read(request.nereid_user):
                #raise abort(404)

            #return iteration

            #if request.is_xhr:
                #return jsonify({
                    #'success': True,
                #})

        if request.is_xhr:
            return jsonify({
                'success': True,
            })

        #if request.is_xhr:
            #response = cls.serialize(iteration)
            #return jsonify(response)
            #return jsonify({
                #'success': True,
                ##'name': ,
                ##'start_date': ,
                ##'end_date': ,
                #'message': "somthing is wrong"
            #})

        return render_template(
            'project/iteration.jinja', iteration=iteration,
            project=project, active_type_name='render_iteration_list'
        )

    @classmethod
    @login_required
    def render_iteration_list(cls, project_id, iteration_id):
        """
        Renders a project's iteration list page
        """
        Iteration = Pool().get('project.iteration')
        project = cls.get_project(project_id)
        state = request.args.get('state', None)
        page = request.args.get('page', 1, int)

        iterations = map(int, Iteration.search([
            ('id', '=', iteration_id),
            ('project.id', '=', project.id)
        ]))

        filter_domain = [
            ('iterations', '=', iterations.id),
            ('parent', '=', project.id),
        ]

        if request.is_xhr:
            iterations = cls.search(filter_domain)
            return jsonify({
                'success': True,
                'domain': filter_domain,
            })

        iterations = Pagination(cls, filter_domain, page, 10)
        return render_template(
            'project/iteration-list.jinja', project=project,
            active_type_name='render_iteration_list', iterations=iterations
        )
