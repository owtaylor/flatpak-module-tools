from setuptools import setup

setup(name='flatpak-module-tools',
      version='0.1',
      description='Tools for creating and maintaining Flatpaks as Fedora modules',
      url='https://pagure.io/flatpak-module-tools',
      author='Owen Taylor',
      author_email='otaylor@redhat.com',
      license='MIT',
      packages=['flatpak_module_tools', 'flatpak_module_tools.commands'],
      package_data={'flatpak_module_tools': ['*.template.yaml']},
      include_package_data=True,
      scripts=['flatpak-module'])
