#!/usr/bin/env python

from setuptools import setup

setup(name='target-bigquery',
      version='1.2.0',
      description='Singer.io target for writing data to Google BigQuery',
      author='RealSelf Business Intelligence',
      url='https://realself.com',
      classifiers=['Programming Language :: Python :: 3 :: Only'],
      py_modules=['target_bigquery'],
      install_requires=[
          'jsonschema==2.6.0',
          'singer-python==1.5.0',
          'google-cloud-bigquery==1.8.1',
          'oauth2client==4.1.3',
      ],
      entry_points='''
          [console_scripts]
          target-bigquery=target_bigquery:main
      ''',
)
