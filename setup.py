from setuptools import setup
import sys

# Only install the CLI for python3. For python2, all we want to work is
# flatpak_builder.py
if sys.version_info[0] == 3:
    entry_points='''
        [console_scripts]
        flatpak-module=flatpak_module_tools.cli:cli
    '''
else:
    entry_points = None

setup(name='flatpak-module-tools',
      version='0.5',
      description='Tools for creating and maintaining Flatpaks as Fedora modules',
      url='https://pagure.io/flatpak-module-tools',
      author='Owen Taylor',
      author_email='otaylor@redhat.com',
      license='MIT',
      packages=['flatpak_module_tools'],
      package_data={'flatpak_module_tools': ['templates/*.j2']},
      include_package_data=True,
      entry_points=entry_points,
      )
