# This file is mostly a derivative work of Buildbot.
#
# Buildbot is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from io import BytesIO

from twisted.internet import threads
from buildbot.util.logger import Logger
from buildbot.interfaces import LatentWorkerCannotSubstantiate
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.worker.docker import (DockerLatentWorker, _handle_stream_line,
                                    docker_py_version, docker)

from .utils import ensure_deferred


log = Logger()


class WorkerMixin:

    def __init__(self, *args, arch=None, tags=tuple(), **kwargs):
        self.arch = arch
        self.tags = tuple(tags)
        super().__init__(*args, **kwargs)


class DockerLatentWorker(WorkerMixin, DockerLatentWorker):
    # License note:
    #    copied from the original implementation with minor modification
    #    to pass runtime configuration to the containers

    def renderWorkerProps(self, build):
        return build.render(
            (self.image, self.dockerfile, self.hostconfig, self.volumes)
        )

    @ensure_deferred
    async def start_instance(self, build):
        if self.instance is not None:
            raise ValueError('instance active')
        args = await self.renderWorkerPropsOnStart(build)
        return await threads.deferToThread(self._thd_start_instance, *args)

    def _thd_start_instance(self, image, dockerfile, hostconfig, volumes):
        docker_client = self._getDockerClient()
        container_name = self.getContainerName()
        # cleanup the old instances
        instances = docker_client.containers(
            all=1,
            filters=dict(name=container_name))
        container_name = '/{0}'.format(container_name)
        for instance in instances:
            if container_name not in instance['Names']:
                continue
            try:
                docker_client.remove_container(instance['Id'], v=True,
                                               force=True)
            except docker.errors.NotFound:
                pass  # that's a race condition

        found = False
        if image is not None:
            found = self._image_exists(docker_client, image)
        else:
            worker_id = id(self)
            worker_name = self.workername
            image = f'{worker_name}_{worker_id}_image'
        if (not found) and (dockerfile is not None):
            log.info(f'Image {image} not found, building it from scratch')
            for line in docker_client.build(
                fileobj=BytesIO(dockerfile.encode('utf-8')),
                tag=image
            ):
                for streamline in _handle_stream_line(line):
                    log.info(streamline)

        imageExists = self._image_exists(docker_client, image)
        if ((not imageExists) or self.alwaysPull) and self.autopull:
            if (not imageExists):
                log.info(f'Image {image} not found, pulling from registry')
            docker_client.pull(image)

        if (not self._image_exists(docker_client, image)):
            log.info(f'Image {image} not found')
            raise LatentWorkerCannotSubstantiate(
                f'Image {image} not found on docker host.'
            )

        volumes, binds = self._thd_parse_volumes(volumes)

        hostconfig['binds'] = binds
        if docker_py_version >= 2.2:
            hostconfig['init'] = True

        instance = docker_client.create_container(
            image,
            self.command,
            name=self.getContainerName(),
            volumes=volumes,
            environment=self.createEnvironment(),
            host_config=docker_client.create_host_config(
                **hostconfig
            )
        )

        if instance.get('Id') is None:
            log.info('Failed to create the container')
            raise LatentWorkerFailedToSubstantiate(
                'Failed to start container'
            )
        shortid = instance['Id'][:6]
        log.info(f'Container created, Id: {shortid}...')
        instance['image'] = image
        self.instance = instance
        docker_client.start(instance)
        log.info('Container started')
        if self.followStartupLogs:
            logs = docker_client.attach(
                container=instance, stdout=True, stderr=True, stream=True)
            for line in logs:
                line = line.strip()
                log.info(f'docker VM {shortid}: {line}')
                if self.conn:
                    break
            del logs
        return [instance['Id'], image]
