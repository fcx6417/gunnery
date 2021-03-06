from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete
from django.core.urlresolvers import reverse
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from backend.tasks import *
from backend.securefile import SecureFileStorage
from core.models import *

class Task(models.Model):
	name = models.CharField(blank=False, max_length=128, validators=[gunnery_name()])
	description = models.TextField(blank=True)
	application = models.ForeignKey(Application, related_name="tasks")
	class Meta:
		unique_together = ("application", "name")

	def get_absolute_url(self):
	    return reverse('task_page', args=[str(self.id)])

	def executions_inline(self):
		return Execution.get_inline_by_task(self.id)

	def parameters_ordered(self):
		return self.parameters.order_by('order')

	def commands_ordered(self):
		return self.commands.order_by('order')
	
class TaskParameter(models.Model):
	task = models.ForeignKey(Task, related_name="parameters")
	name = models.CharField(blank=False, max_length=128, validators=[gunnery_name()])
	default_value = models.CharField(max_length=128)
	description = models.TextField(blank=True)
	order = models.IntegerField()
	
class TaskCommand(models.Model):
	task = models.ForeignKey(Task, related_name="commands")
	command = models.TextField(blank=False)
	roles = models.ManyToManyField(ServerRole, related_name="commands")
	order = models.IntegerField()
	

	
class Execution(models.Model):
	PENDING = 3
	RUNNING = 0
	SUCCESS = 1
	FAILED = 2
	STATUS_CHOICES = (
		(PENDING, 'pending'),
		(RUNNING, 'running'),
		(SUCCESS, 'success'),
		(FAILED, 'failed'),
	)
	task = models.ForeignKey(Task, related_name="executions")
	time_created = models.DateTimeField(auto_now_add=True)
	time_start = models.DateTimeField(blank=True, null=True)
	time_end = models.DateTimeField(blank=True, null=True)
	time = models.IntegerField(blank=True, null=True)
	environment = models.ForeignKey(Environment, related_name="executions")
	user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="executions")
	status = models.IntegerField(choices=STATUS_CHOICES, default=PENDING)
	def get_absolute_url(self):
		return reverse('execution_page', args=[str(self.id)])

	def save(self, *args, **kwargs):
		is_new = not self.id
		super(Execution, self).save(*args, **kwargs)
		if not is_new:
			return
		for command in self.task.commands_ordered():
			self._create_execution_commands(command)

	def start(self):
		from backend.tasks import ExecutionTask
		ExecutionTask().delay(execution_id=self.id)

	def _create_execution_commands(self, command):
		parsed_command = command.command
		execution_command = ExecutionCommand(execution=self, command=parsed_command)
		execution_command.save()
		for role in command.roles.all():
			execution_command.roles.add(role)
		execution_command.save()
		self._create_execution_commands_servers(command, execution_command)

	def _create_execution_commands_servers(self, command, execution_command):
		for server in self.environment.servers.filter(roles__in=command.roles.all()):
			execution_command_server = ExecutionCommandServer(
				execution_command=execution_command,
				server=server)
			execution_command_server.save()

	@staticmethod
	def get_inline_by_query(**kwargs):
		return list(Execution.objects.filter(**kwargs).prefetch_related('user').prefetch_related('task').prefetch_related('environment').order_by('-time_created')[:4])

	@staticmethod
	def get_inline_by_application(id):
		return Execution.get_inline_by_query(task__application_id=id)

	@staticmethod
	def get_inline_by_environment(id):
		return Execution.get_inline_by_query(environment_id=id)

	@staticmethod
	def get_inline_by_task(id):
		return Execution.get_inline_by_query(task_id=id)

	@staticmethod
	def get_inline_by_user(id):
		return Execution.get_inline_by_query(user_id=id)

class ExecutionParameter(models.Model):
	execution = models.ForeignKey(Execution, related_name="parameters")
	name = models.CharField(blank=False, max_length=128)
	value = models.CharField(max_length=128)

class ExecutionCommand(models.Model):
	execution = models.ForeignKey(Execution, related_name="commands")
	command = models.TextField()
	roles = models.ManyToManyField(ServerRole)

class ExecutionCommandServer(models.Model):
	PENDING = 3
	RUNNING = 0
	SUCCESS = 1
	FAILED = 2
	STATUS_CHOICES = (
		(PENDING, 'pending'),
		(RUNNING, 'running'),
		(SUCCESS, 'success'),
		(FAILED, 'failed'),
	)
	execution_command = models.ForeignKey(ExecutionCommand, related_name="servers")
	status = models.IntegerField(choices=STATUS_CHOICES, default=PENDING)
	time_start = models.DateTimeField(blank=True, null=True)
	time_end = models.DateTimeField(blank=True, null=True)
	time = models.IntegerField(blank=True, null=True)
	return_code = models.IntegerField(blank=True, null=True)
	server = models.ForeignKey(Server)
	# @todo store host, and ip here instead of relation to Server model
	output = models.TextField(blank=True)
	def get_live_log_output(self):
		live_logs = self.live_logs.values_list('output', flat=True)
		return ''.join(live_logs)

class ExecutionLiveLog(models.Model):
	execution = models.ForeignKey(Execution, related_name="live_logs")
	event = models.CharField(max_length=128)
	data = models.TextField(blank=True)


class ParameterParser(object):
	parameter_format = '${%s}'
	global_parameters = {
		'gun_application': 'application name',
		'gun_environment': 'environment name',
		'gun_task': 'task name',
		'gun_user': 'user email',
		'gun_time': 'execution start timestamp'
	}

	def __init__(self, execution):
		self.execution = execution
		import calendar
		self.global_parameter_values = {
			'gun_application': self.execution.task.application.name,
			'gun_environment': self.execution.environment.name,
			'gun_task': self.execution.task.name,
			'gun_user': self.execution.user.email,
			'gun_time': str(calendar.timegm(self.execution.time_created.utctimetuple()))
		}

	def process(self, cmd):
		cmd = self._process_global_parameters(cmd)
		cmd = self._process_parameters(cmd)
		return cmd

	def _process_global_parameters(self, cmd):
		for name, value in self.global_parameter_values.items():
			cmd = cmd.replace(self.parameter_format % name, value)
		return cmd

	def _process_parameters(self, cmd):
		execution_params = self.execution.parameters.all()
		for param in execution_params:
			cmd = cmd.replace(self.parameter_format % param.name, param.value)
		return cmd
