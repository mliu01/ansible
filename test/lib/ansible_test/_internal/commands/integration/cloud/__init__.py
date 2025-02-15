"""Plugin system for cloud providers and environments for use in integration tests."""
from __future__ import annotations

import abc
import atexit
import datetime
import os
import re
import tempfile
import time
import typing as t

from ....encoding import (
    to_bytes,
)

from ....io import (
    read_text_file,
)

from ....util import (
    ANSIBLE_TEST_CONFIG_ROOT,
    ApplicationError,
    display,
    import_plugins,
    load_plugins,
    cache,
)

from ....util_common import (
    ResultType,
    write_json_test_results,
)

from ....target import (
    IntegrationTarget,
)

from ....config import (
    IntegrationConfig,
    TestConfig,
)

from ....ci import (
    get_ci_provider,
)

from ....data import (
    data_context,
)

from ....docker_util import (
    docker_available,
)


@cache
def get_cloud_plugins():  # type: () -> t.Tuple[t.Dict[str, t.Type[CloudProvider]], t.Dict[str, t.Type[CloudEnvironment]]]
    """Import cloud plugins and load them into the plugin dictionaries."""
    import_plugins('commands/integration/cloud')

    providers = {}
    environments = {}

    load_plugins(CloudProvider, providers)
    load_plugins(CloudEnvironment, environments)

    return providers, environments


@cache
def get_provider_plugins():  # type: () -> t.Dict[str, t.Type[CloudProvider]]
    """Return a dictionary of the available cloud provider plugins."""
    return get_cloud_plugins()[0]


@cache
def get_environment_plugins():  # type: () -> t.Dict[str, t.Type[CloudEnvironment]]
    """Return a dictionary of the available cloud environment plugins."""
    return get_cloud_plugins()[1]


def get_cloud_platforms(args, targets=None):  # type: (TestConfig, t.Optional[t.Tuple[IntegrationTarget, ...]]) -> t.List[str]
    """Return cloud platform names for the specified targets."""
    if isinstance(args, IntegrationConfig):
        if args.list_targets:
            return []

    if targets is None:
        cloud_platforms = set(args.metadata.cloud_config or [])
    else:
        cloud_platforms = set(get_cloud_platform(target) for target in targets)

    cloud_platforms.discard(None)

    return sorted(cloud_platforms)


def get_cloud_platform(target):  # type: (IntegrationTarget) -> t.Optional[str]
    """Return the name of the cloud platform used for the given target, or None if no cloud platform is used."""
    cloud_platforms = set(a.split('/')[1] for a in target.aliases if a.startswith('cloud/') and a.endswith('/') and a != 'cloud/')

    if not cloud_platforms:
        return None

    if len(cloud_platforms) == 1:
        cloud_platform = cloud_platforms.pop()

        if cloud_platform not in get_provider_plugins():
            raise ApplicationError('Target %s aliases contains unknown cloud platform: %s' % (target.name, cloud_platform))

        return cloud_platform

    raise ApplicationError('Target %s aliases contains multiple cloud platforms: %s' % (target.name, ', '.join(sorted(cloud_platforms))))


def get_cloud_providers(args, targets=None):  # type: (IntegrationConfig, t.Optional[t.Tuple[IntegrationTarget, ...]]) -> t.List[CloudProvider]
    """Return a list of cloud providers for the given targets."""
    return [get_provider_plugins()[p](args) for p in get_cloud_platforms(args, targets)]


def get_cloud_environment(args, target):  # type: (IntegrationConfig, IntegrationTarget) -> t.Optional[CloudEnvironment]
    """Return the cloud environment for the given target, or None if no cloud environment is used for the target."""
    cloud_platform = get_cloud_platform(target)

    if not cloud_platform:
        return None

    return get_environment_plugins()[cloud_platform](args)


def cloud_filter(args, targets):  # type: (IntegrationConfig, t.Tuple[IntegrationTarget, ...]) -> t.List[str]
    """Return a list of target names to exclude based on the given targets."""
    if args.metadata.cloud_config is not None:
        return []  # cloud filter already performed prior to delegation

    exclude = []

    for provider in get_cloud_providers(args, targets):
        provider.filter(targets, exclude)

    return exclude


def cloud_init(args, targets):  # type: (IntegrationConfig, t.Tuple[IntegrationTarget, ...]) -> None
    """Initialize cloud plugins for the given targets."""
    if args.metadata.cloud_config is not None:
        return  # cloud configuration already established prior to delegation

    args.metadata.cloud_config = {}

    results = {}

    for provider in get_cloud_providers(args, targets):
        args.metadata.cloud_config[provider.platform] = {}

        start_time = time.time()
        provider.setup()
        end_time = time.time()

        results[provider.platform] = dict(
            platform=provider.platform,
            setup_seconds=int(end_time - start_time),
            targets=[target.name for target in targets],
        )

    if not args.explain and results:
        result_name = '%s-%s.json' % (
            args.command, re.sub(r'[^0-9]', '-', str(datetime.datetime.utcnow().replace(microsecond=0))))

        data = dict(
            clouds=results,
        )

        write_json_test_results(ResultType.DATA, result_name, data)


class CloudBase(metaclass=abc.ABCMeta):
    """Base class for cloud plugins."""
    _CONFIG_PATH = 'config_path'
    _RESOURCE_PREFIX = 'resource_prefix'
    _MANAGED = 'managed'
    _SETUP_EXECUTED = 'setup_executed'

    def __init__(self, args):  # type: (IntegrationConfig) -> None
        self.args = args
        self.platform = self.__module__.rsplit('.', 1)[-1]

        def config_callback(files):  # type: (t.List[t.Tuple[str, str]]) -> None
            """Add the config file to the payload file list."""
            if self.platform not in self.args.metadata.cloud_config:
                return  # platform was initialized, but not used -- such as being skipped due to all tests being disabled

            if self._get_cloud_config(self._CONFIG_PATH, ''):
                pair = (self.config_path, os.path.relpath(self.config_path, data_context().content.root))

                if pair not in files:
                    display.info('Including %s config: %s -> %s' % (self.platform, pair[0], pair[1]), verbosity=3)
                    files.append(pair)

        data_context().register_payload_callback(config_callback)

    @property
    def setup_executed(self):  # type: () -> bool
        """True if setup has been executed, otherwise False."""
        return self._get_cloud_config(self._SETUP_EXECUTED, False)

    @setup_executed.setter
    def setup_executed(self, value):  # type: (bool) -> None
        """True if setup has been executed, otherwise False."""
        self._set_cloud_config(self._SETUP_EXECUTED, value)

    @property
    def config_path(self):  # type: () -> str
        """Path to the configuration file."""
        return os.path.join(data_context().content.root, self._get_cloud_config(self._CONFIG_PATH))

    @config_path.setter
    def config_path(self, value):  # type: (str) -> None
        """Path to the configuration file."""
        self._set_cloud_config(self._CONFIG_PATH, value)

    @property
    def resource_prefix(self):  # type: () -> str
        """Resource prefix."""
        return self._get_cloud_config(self._RESOURCE_PREFIX)

    @resource_prefix.setter
    def resource_prefix(self, value):  # type: (str) -> None
        """Resource prefix."""
        self._set_cloud_config(self._RESOURCE_PREFIX, value)

    @property
    def managed(self):  # type: () -> bool
        """True if resources are managed by ansible-test, otherwise False."""
        return self._get_cloud_config(self._MANAGED)

    @managed.setter
    def managed(self, value):  # type: (bool) -> None
        """True if resources are managed by ansible-test, otherwise False."""
        self._set_cloud_config(self._MANAGED, value)

    def _get_cloud_config(self, key, default=None):  # type: (str, t.Optional[t.Union[str, int, bool]]) -> t.Union[str, int, bool]
        """Return the specified value from the internal configuration."""
        if default is not None:
            return self.args.metadata.cloud_config[self.platform].get(key, default)

        return self.args.metadata.cloud_config[self.platform][key]

    def _set_cloud_config(self, key, value):  # type: (str, t.Union[str, int, bool]) -> None
        """Set the specified key and value in the internal configuration."""
        self.args.metadata.cloud_config[self.platform][key] = value


class CloudProvider(CloudBase):
    """Base class for cloud provider plugins. Sets up cloud resources before delegation."""
    def __init__(self, args, config_extension='.ini'):  # type: (IntegrationConfig, str) -> None
        super().__init__(args)

        self.ci_provider = get_ci_provider()
        self.remove_config = False
        self.config_static_name = 'cloud-config-%s%s' % (self.platform, config_extension)
        self.config_static_path = os.path.join(data_context().content.integration_path, self.config_static_name)
        self.config_template_path = os.path.join(ANSIBLE_TEST_CONFIG_ROOT, '%s.template' % self.config_static_name)
        self.config_extension = config_extension

        self.uses_config = False
        self.uses_docker = False

    def filter(self, targets, exclude):  # type: (t.Tuple[IntegrationTarget, ...], t.List[str]) -> None
        """Filter out the cloud tests when the necessary config and resources are not available."""
        if not self.uses_docker and not self.uses_config:
            return

        if self.uses_docker and docker_available():
            return

        if self.uses_config and os.path.exists(self.config_static_path):
            return

        skip = 'cloud/%s/' % self.platform
        skipped = [target.name for target in targets if skip in target.aliases]

        if skipped:
            exclude.append(skip)

            if not self.uses_docker and self.uses_config:
                display.warning('Excluding tests marked "%s" which require config (see "%s"): %s'
                                % (skip.rstrip('/'), self.config_template_path, ', '.join(skipped)))
            elif self.uses_docker and not self.uses_config:
                display.warning('Excluding tests marked "%s" which requires container support: %s'
                                % (skip.rstrip('/'), ', '.join(skipped)))
            elif self.uses_docker and self.uses_config:
                display.warning('Excluding tests marked "%s" which requires container support or config (see "%s"): %s'
                                % (skip.rstrip('/'), self.config_template_path, ', '.join(skipped)))

    def setup(self):  # type: () -> None
        """Setup the cloud resource before delegation and register a cleanup callback."""
        self.resource_prefix = self.ci_provider.generate_resource_prefix()
        self.resource_prefix = re.sub(r'[^a-zA-Z0-9]+', '-', self.resource_prefix)[:63].lower().rstrip('-')

        atexit.register(self.cleanup)

    def cleanup(self):  # type: () -> None
        """Clean up the cloud resource and any temporary configuration files after tests complete."""
        if self.remove_config:
            os.remove(self.config_path)

    def _use_static_config(self):  # type: () -> bool
        """Use a static config file if available. Returns True if static config is used, otherwise returns False."""
        if os.path.isfile(self.config_static_path):
            display.info('Using existing %s cloud config: %s' % (self.platform, self.config_static_path), verbosity=1)
            self.config_path = self.config_static_path
            static = True
        else:
            static = False

        self.managed = not static

        return static

    def _write_config(self, content):  # type: (t.Text) -> None
        """Write the given content to the config file."""
        prefix = '%s-' % os.path.splitext(os.path.basename(self.config_static_path))[0]

        with tempfile.NamedTemporaryFile(dir=data_context().content.integration_path, prefix=prefix, suffix=self.config_extension, delete=False) as config_fd:
            filename = os.path.join(data_context().content.integration_path, os.path.basename(config_fd.name))

            self.config_path = filename
            self.remove_config = True

            display.info('>>> Config: %s\n%s' % (filename, content.strip()), verbosity=3)

            config_fd.write(to_bytes(content))
            config_fd.flush()

    def _read_config_template(self):  # type: () -> t.Text
        """Read and return the configuration template."""
        lines = read_text_file(self.config_template_path).splitlines()
        lines = [line for line in lines if not line.startswith('#')]
        config = '\n'.join(lines).strip() + '\n'
        return config

    @staticmethod
    def _populate_config_template(template, values):  # type: (t.Text, t.Dict[str, str]) -> t.Text
        """Populate and return the given template with the provided values."""
        for key in sorted(values):
            value = values[key]
            template = template.replace('@%s' % key, value)

        return template


class CloudEnvironment(CloudBase):
    """Base class for cloud environment plugins. Updates integration test environment after delegation."""
    def setup_once(self):  # type: () -> None
        """Run setup if it has not already been run."""
        if self.setup_executed:
            return

        self.setup()
        self.setup_executed = True

    def setup(self):  # type: () -> None
        """Setup which should be done once per environment instead of once per test target."""

    @abc.abstractmethod
    def get_environment_config(self):  # type: () -> CloudEnvironmentConfig
        """Return environment configuration for use in the test environment after delegation."""

    def on_failure(self, target, tries):  # type: (IntegrationTarget, int) -> None
        """Callback to run when an integration target fails."""


class CloudEnvironmentConfig:
    """Configuration for the environment."""
    def __init__(self,
                 env_vars=None,  # type: t.Optional[t.Dict[str, str]]
                 ansible_vars=None,  # type: t.Optional[t.Dict[str, t.Any]]
                 module_defaults=None,  # type: t.Optional[t.Dict[str, t.Dict[str, t.Any]]]
                 callback_plugins=None,  # type: t.Optional[t.List[str]]
                 ):
        self.env_vars = env_vars
        self.ansible_vars = ansible_vars
        self.module_defaults = module_defaults
        self.callback_plugins = callback_plugins
