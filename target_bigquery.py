#!/usr/bin/env python3

import argparse
import io
import sys
import json
import logging
import collections
import threading
import http.client
import urllib
import pkg_resources

from jsonschema import validate
import singer

from oauth2client import tools
from tempfile import TemporaryFile

from google.cloud import bigquery
from google.cloud.bigquery.job import SourceFormat
from google.cloud.bigquery import Dataset
from google.cloud.bigquery import SchemaField
from google.cloud.bigquery import LoadJobConfig
from google.api_core import exceptions


logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
logger = singer.get_logger()

SCOPES = ['https://www.googleapis.com/auth/bigquery','https://www.googleapis.com/auth/bigquery.insertdata']
CLIENT_SECRET_FILE = 'client_secret.json'
APPLICATION_NAME = 'Singer BigQuery Target'

StreamMeta = collections.namedtuple('StreamMeta', ['schema', 'key_properties', 'bookmark_properties'])

def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        logger.debug('Emitting state {}'.format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()

def clear_dict_hook(items):
    return {k: v if v is not None else '' for k, v in items}

def define_schema(field, name):
    # FIXME : better string cleaning
    schema_name = name.replace(" ", "_").replace(".", "_")

    schema_type = "STRING"
    schema_mode = "NULLABLE"
    schema_description = None
    schema_fields = ()

    if 'type' not in field and 'anyOf' in field:
        for types in field['anyOf']:
            if types['type'] == 'null':
                schema_mode = 'NULLABLE'
            else:
                field = types

    if isinstance(field['type'], list):
        # if field['type'][0] == "null":
        """
        if "null" in field['type']:
            schema_mode = 'NULLABLE'
        else:
            schema_mode = 'required'
        """
        schema_mode = 'NULLABLE'
        # schema_type = field['type'][0]
        schema_type = [t for t in field['type'] if t != "null"][0]
    else:
        schema_type = field['type']
    if schema_type == "object":
        schema_type = "RECORD"
        schema_fields = tuple(build_schema(field))
    if schema_type == "array":
        types = field.get('items', {}).get('type', [])
        schema_type = [t for t in types if t != "null"][0]
        schema_mode = "REPEATED"
        if schema_type == "object":
          schema_type = "RECORD"
          schema_fields = tuple(build_schema(field.get('items')))


    if schema_type == "string":
        if "format" in field:
            if field['format'] == "date-time":
                schema_type = "timestamp"

    if schema_type == 'number':
        schema_type = 'FLOAT'

    return (schema_name, schema_type, schema_mode, schema_description, schema_fields)

def build_schema(schema):
    import re
    SCHEMA = []
    for key in schema['properties'].keys():

        schema_name, schema_type, schema_mode, schema_description, schema_fields = define_schema(schema['properties'][key], key)
        # TODO : what to do if name is invalid ?
        if not re.match("[a-zA-Z0-9_]+", schema_name):
            continue
        SCHEMA.append(SchemaField(schema_name, schema_type, schema_mode, schema_description, schema_fields))

    return SCHEMA

def persist_lines_job(project_id, dataset_id, lines=None):
    state = None
    schemas = {}
    key_properties = {}
    tables = {}
    rows = {}
    errors = {}

    bigquery_client = bigquery.Client(project=project_id)

    # try:
    #     dataset = bigquery_client.create_dataset(Dataset(dataset_ref)) or Dataset(dataset_ref)
    # except exceptions.Conflict:
    #     pass

    for line in lines:
        try:
            msg = singer.parse_message(line)
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(line))
            raise

        if isinstance(msg, singer.RecordMessage):
            if msg.stream not in schemas:
                raise Exception("A record for stream {} was encountered before a corresponding schema".format(msg.stream))

            schema = schemas[msg.stream]

            validate(msg.record, schema)

            dat = bytes(str(json.loads(json.dumps(msg.record), object_pairs_hook=clear_dict_hook)) + '\n', 'UTF-8')

            rows[msg.stream].write(dat)
            #rows[msg.stream].write(bytes(str(msg.record) + '\n', 'UTF-8'))

            state = None

        elif isinstance(msg, singer.StateMessage):
            logger.debug('Setting state to {}'.format(msg.value))
            state = msg.value

        elif isinstance(msg, singer.SchemaMessage):
            table = msg.stream
            schemas[table] = msg.schema
            key_properties[table] = msg.key_properties
            #tables[table] = bigquery.Table(dataset.table(table), schema=build_schema(schemas[table]))
            rows[table] = TemporaryFile(mode='w+b')
            errors[table] = None
            # try:
            #     tables[table] = bigquery_client.create_table(tables[table])
            # except exceptions.Conflict:
            #     pass

        elif isinstance(msg, singer.ActivateVersionMessage):
            # This is experimental and won't be used yet
            pass

        else:
            raise Exception("Unrecognized message {}".format(msg))

    for table in rows.keys():
        table_ref = bigquery_client.dataset(dataset_id).table(table)
        SCHEMA = build_schema(schemas[table])
        load_config = LoadJobConfig()
        load_config.schema = SCHEMA
        load_config.source_format = SourceFormat.NEWLINE_DELIMITED_JSON
        rows[table].seek(0)
        logger.info("loading {} to Bigquery.\n".format(table))
        load_job = bigquery_client.load_table_from_file(
            rows[table], table_ref, job_config=load_config)
        logger.info("loading job {}".format(load_job.job_id))
        logger.info(load_job.result())


    # for table in errors.keys():
    #     if not errors[table]:
    #         print('Loaded {} row(s) into {}:{}'.format(rows[table], dataset_id, table), tables[table].path)
    #     else:
    #         print('Errors:', errors[table], sep=" ")

    return state

def persist_lines_stream(config, project_id, dataset_id, lines=None):
    state = None
    schemas = {}
    key_properties = {}
    tables = {}
    rows = {}
    errors = {}

    from google.oauth2.credentials import Credentials
    credentials = Credentials(
            token=config["token"],
            refresh_token=config["refresh_token"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            token_uri=config["token_uri"]
            )

    bigquery_client = bigquery.Client(project=project_id, credentials=credentials)

    dataset_ref = bigquery_client.dataset(dataset_id)
    dataset = Dataset(dataset_ref)
    try:
        dataset = bigquery_client.create_dataset(Dataset(dataset_ref)) or Dataset(dataset_ref)
    except exceptions.Conflict:
        pass

    records = {}
    for line in lines:
        try:
            msg = singer.parse_message(line)
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(line))
            raise

        if isinstance(msg, singer.RecordMessage):
            if msg.stream not in schemas:
                raise Exception("A record for stream {} was encountered before a corresponding schema".format(msg.stream))

            schema = schemas[msg.stream]

            validate(msg.record, schema)
            try:
                records[msg.stream].append(msg.record)
            except KeyError:
                records[msg.stream] = [msg.record]
            rows[msg.stream] += 1

            state = None

        elif isinstance(msg, singer.StateMessage):
            logger.debug('Setting state to {}'.format(msg.value))
            state = msg.value

        elif isinstance(msg, singer.SchemaMessage):
            table = msg.stream
            schemas[table] = msg.schema
            key_properties[table] = msg.key_properties
            tables[table] = bigquery.Table(dataset.table(table), schema=build_schema(schemas[table]))
            rows[table] = 0
            errors[table] = None
            try:
                tables[table] = bigquery_client.create_table(tables[table])
            except exceptions.Conflict:
                pass

        elif isinstance(msg, singer.ActivateVersionMessage):
            # This is experimental and won't be used yet
            pass

        else:
            raise Exception("Unrecognized message {}".format(msg))

    # flush all rows only once
    for stream, _rows in records.items():
        errors[stream] = bigquery_client.insert_rows(tables[stream], _rows)

    for table in errors.keys():
        if not errors[table]:
            print('Loaded {} row(s) into {}:{}'.format(rows[table], dataset_id, table), tables[table].path, file=sys.stderr)
        else:
            print('Errors:', errors[table], sep=" ", file=sys.stderr)

    return state

def collect():
    try:
        version = pkg_resources.get_distribution('target-bigquery').version
        conn = http.client.HTTPConnection('collector.singer.io', timeout=10)
        conn.connect()
        params = {
            'e': 'se',
            'aid': 'singer',
            'se_ca': 'target-bigquery',
            'se_ac': 'open',
            'se_la': version,
        }
        conn.request('GET', '/i?' + urllib.parse.urlencode(params))
        conn.getresponse()
        conn.close()
    except:
        logger.debug('Collection request failed')

def main():

    try:
        parser = argparse.ArgumentParser(parents=[tools.argparser])
        parser.add_argument('-c', '--config', help='Config file', required=True)
        flags = parser.parse_args()

    except ImportError:
        flags = None


    with open(flags.config) as input:
        config = json.load(input)

    if not config.get('disable_collection', False):
        logger.info('Sending version information to stitchdata.com. ' +
                    'To disable sending anonymous usage data, set ' +
                    'the config parameter "disable_collection" to true')
        threading.Thread(target=collect).start()

    input = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')

    state = persist_lines_stream(config, config['project_id'], config['dataset_id'], input) if config.get('stream_data', True) else persist_lines_job(config['project_id'], config['dataset_id'], input)
    emit_state(state)
    logger.debug("Exiting normally")


if __name__ == '__main__':
    main()
