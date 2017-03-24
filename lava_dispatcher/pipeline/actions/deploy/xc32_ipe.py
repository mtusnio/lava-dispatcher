import os
from time import sleep
from lava_dispatcher.pipeline.logical import Deployment
from lava_dispatcher.pipeline.action import (
    Action,
    Pipeline,
    JobError,
    InfrastructureError,
)
from lava_dispatcher.pipeline.actions.deploy import DeployAction


class IPE(Deployment):
    def __init__(self, parent, parameters):
        super(IPE, self).__init__(parent)
        self.action = ProgramAction()
        self.action.section = self.action_type
        self.action.job = self.job
        parent.add_action(self.action, parameters)


class ProgramAction(DeployAction):
    def __init__(self):
        super(ProgramAction, self).__init__()

    def run(self, connection, max_end_time, args=None):
        connection = super(ProgramAction, self).run(connection, max_end_time, args)
        flash_command = [ "ipecmd.sh", "-p32MX470F512H", "-E", "-C", "-F/main.hex" "-M", "-TPPK3", "-Y" ]
        out = self.run_command(flash_command)
        return connection
