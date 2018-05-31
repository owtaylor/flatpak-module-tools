from setuptools import setup

setup(name='flatpak-module-tools',
      version='0.3',
      description='Tools for creating and maintaining Flatpaks as Fedora modules',
      url='https://pagure.io/flatpak-module-tools',
      author='Owen Taylor',
      author_email='otaylor@redhat.com',
      license='MIT',
      packages=['flatpak_module_tools'],
      package_data={'flatpak_module_tools': ['templates/*.j2']},
      include_package_data=True,
       entry_points='''
          [console_scripts]
          flatpak-module=flatpak_module_tools.cli:cli
      ''',)
