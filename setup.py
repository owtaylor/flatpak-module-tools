from setuptools import setup

entry_points = '''
    [console_scripts]
    flatpak-module=flatpak_module_tools.cli:cli
    flatpak-module-depchase=flatpak_module_tools.depchase.cli:cli
'''

setup(name='flatpak-module-tools',
      version='0.14',
      description='Tools for creating and maintaining Flatpaks as Fedora modules',
      url='https://pagure.io/flatpak-module-tools',
      author='Owen Taylor',
      author_email='otaylor@redhat.com',
      license='MIT',
      packages=['flatpak_module_tools', 'tests'],
      package_data={'flatpak_module_tools': ['config.yaml', 'templates/*.j2']},
      include_package_data=True,
      entry_points=entry_points,
      )
