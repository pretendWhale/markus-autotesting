import os
import pprint
import subprocess

import psycopg2
from markus_utils import MarkusUtilsMixin
# from markusapi import Markus


class MarkusSQLTester(MarkusUtilsMixin):

    SCHEMA_FILE = 'schema.ddl'
    DATASET_DIR = 'datasets'
    QUERY_DIR = 'queries'
    ERROR_MSGS = {'no_submission': 'Submission file {} not found',
                  'no_submission_order': 'Ordering required, order file {} not found',
                  'bad_col_count': 'Expected {} columns instead of {}',
                  'bad_col_name': "Expected column {} to have name '{}' instead of '{}'",
                  'bad_col_type': "The type of values in column '{}' does not match the expected type",
                  'bad_col_type_maybe': "The type of values in column '{}' does not match the expected type (but no row"
                                        "values are available to check whether they could be compatible types)",
                  'bad_row_count': 'Expected {} rows instead of {}',
                  'bad_row_content_no_order': 'Expected to find a row {} in the unordered results',
                  'bad_row_content_order': 'Expected row {} in the ordered results to be {} instead of {}'}

    def __init__(self, oracle_database, test_database, user_name, user_password, path_to_solution, schema_name, specs,
                 order_bys={}, output_filename='result.txt'):
        self.oracle_database = oracle_database
        self.test_database = test_database
        self.user_name = user_name
        self.user_password = user_password
        self.path_to_solution = path_to_solution
        self.schema_name = schema_name
        self.specs = specs
        self.order_bys = order_bys
        self.output_filename = output_filename
        self.oracle_connection = None
        self.oracle_cursor = None
        self.test_connection = None
        self.test_cursor = None

    def init_db(self):
        self.oracle_connection = psycopg2.connect(database=self.oracle_database, user=self.user_name,
                                                  password=self.user_password, host='localhost')
        self.oracle_cursor = self.oracle_connection.cursor()
        self.test_connection = psycopg2.connect(database=self.test_database, user=self.user_name,
                                                password=self.user_password, host='localhost')
        self.test_cursor = self.test_connection.cursor()

    def close_db(self):
        if self.test_cursor:
            self.test_cursor.close()
        if self.test_connection:
            self.test_connection.close()
        if self.oracle_cursor:
            self.oracle_cursor.close()
        if self.oracle_connection:
            self.oracle_connection.close()

    @staticmethod
    def oracle_query(data_name, test_name, order_by=None):
        query = 'SELECT * FROM %(schema)s.%(table)s'
        query_vars = {'schema': psycopg2.extensions.AsIs(data_name.lower()),
                      'table': psycopg2.extensions.AsIs('oracle_{}'.format(test_name.lower()))}
        if order_by:
            query += ' ORDER BY %(order)s'
            query_vars['order'] = psycopg2.extensions.AsIs(order_by)

        return query, query_vars

    def get_oracle_results(self, data_name, test_name, order_by=None):
        query, query_vars = self.oracle_query(data_name=data_name, test_name=test_name, order_by=order_by)
        self.oracle_cursor.execute(query, query_vars)
        self.oracle_connection.commit()
        oracle_results = self.oracle_cursor.fetchall()

        return oracle_results

    def set_test_schema(self, data_file):
        self.test_cursor.execute('DROP SCHEMA IF EXISTS %(schema)s CASCADE',
                                 {'schema': psycopg2.extensions.AsIs(self.schema_name)})
        self.test_cursor.execute('CREATE SCHEMA %(schema)s', {'schema': psycopg2.extensions.AsIs(self.schema_name)})
        self.test_cursor.execute('SET search_path TO %(schema)s',
                                 {'schema': psycopg2.extensions.AsIs(self.schema_name)})
        with open(os.path.join(self.path_to_solution, self.SCHEMA_FILE)) as schema_open:
            schema = schema_open.read()
            self.test_cursor.execute(schema)
        with open(os.path.join(self.path_to_solution, self.DATASET_DIR, data_file)) as data_open:
            data = data_open.read()
            self.test_cursor.execute(data)
        self.test_connection.commit()

    def test_query(self, test_name):
        query = 'SELECT * FROM %(schema)s.%(table)s'
        query_vars = {'schema': psycopg2.extensions.AsIs(self.schema_name),
                      'table': psycopg2.extensions.AsIs(test_name.lower())}

        return query, query_vars

    def get_test_results(self, test_name, sql_file, sql_order_file=None):
        with open(sql_file) as sql_open:
            sql = sql_open.read()
            self.test_cursor.execute(sql)
            self.test_connection.commit()
        if sql_order_file:
            with open(sql_order_file) as sql_order_open:
                sql = sql_order_open.read()
                self.test_cursor.execute(sql)
        else:
            query, query_vars = self.test_query(test_name=test_name)
            self.test_cursor.execute(query, query_vars)
        self.test_connection.commit()
        test_results = self.test_cursor.fetchall()

        return test_results

    def check_results(self, oracle_results, test_results, order_on=True):

        oracle_columns = self.oracle_cursor.description
        test_columns = self.test_cursor.description

        # check 1: column count
        oracle_num_columns = len(oracle_columns)
        test_num_columns = len(test_columns)
        if oracle_num_columns != test_num_columns:
            return self.ERROR_MSGS['bad_col_count'].format(oracle_num_columns, test_num_columns), 'fail'

        check_column_types = []
        for i, oracle_column in enumerate(oracle_columns):
            # check 2: column names + order
            if test_columns[i].name != oracle_column.name:
                return self.ERROR_MSGS['bad_col_name'].format(i, oracle_column.name, test_columns[i].name), 'fail'
            # check 3: column types
            # (strictly different PostgreSQL oid types can be Python-compatible instead, e.g. varchar and text, so this
            # check will be deferred to row analysis)
            if test_columns[i].type_code != oracle_column.type_code:
                if not oracle_results and not test_results:
                    return self.ERROR_MSGS['bad_col_type_maybe'].format(oracle_column.name), 'fail'
                if not oracle_results or not test_results:  # will fail on check 4 instead
                    continue
                check_column_types.append(i)

        # check 4: row count
        oracle_num_results = len(oracle_results)
        test_num_results = len(test_results)
        if oracle_num_results != test_num_results:
            return self.ERROR_MSGS['bad_row_count'].format(oracle_num_results, test_num_results), 'fail'

        for i, oracle_row in enumerate(oracle_results):
            if order_on:
                test_row = test_results[i]
            else:
                # check 5, unordered variant: row contents
                try:
                    t = test_results.index(oracle_row)
                    test_row = test_results.pop(t)
                except ValueError:
                    return self.ERROR_MSGS['bad_row_content_no_order'].format(oracle_row), 'fail'
            checked_column_types = []
            for j in check_column_types:
                # check 3: column types compatibility deferred trigger
                oracle_value = oracle_row[j]
                test_value = test_row[j]
                if test_value is None or oracle_value is None:  # try next row for types
                    continue
                if type(test_value) is not type(oracle_value):
                    return self.ERROR_MSGS['bad_col_type'].format(oracle_columns[j].name), 'fail'
                checked_column_types.append(j)
            check_column_types = [j for j in check_column_types if j not in checked_column_types]
            # check 5, ordered variant: row contents + order
            if order_on and oracle_row != test_row:
                return self.ERROR_MSGS['bad_row_content_order'].format(i, oracle_row, test_results[i]), 'fail'
        # check 3: column types compatibility deferred trigger
        if check_column_types:
            return self.ERROR_MSGS['bad_col_type_maybe'].format(oracle_columns[check_column_types[0]].name), 'fail'

        # all good
        return '', 'pass'

    @staticmethod
    def print_file_header(output_open, data_name, test_name, status, feedback):
        # header
        output_open.write('========== {} + {}: {} ==========\n'.format(test_name, data_name, status.upper()))
        # test output
        if feedback:
            output_open.write('Feedback: {}\n'.format(feedback))

    def print_file_psql(self, output_open, data_name, test_name, status, feedback, order_by=None, sql_order_file=None):
        # header
        self.print_file_header(output_open=output_open, data_name=data_name, test_name=test_name, status=status,
                               feedback=feedback)
        # table dump using psql if not passed
        if status != 'pass':
            oracle_query, oracle_vars = self.oracle_query(data_name=data_name, test_name=test_name, order_by=order_by)
            oracle_command = ['psql', '-U', self.user_name, '-d', self.oracle_database, '-h', 'localhost', '-c',
                              self.oracle_cursor.mogrify(oracle_query, oracle_vars)]
            test_command = ['psql', '-U', self.user_name, '-d', self.test_database, '-h', 'localhost']
            if order_by and sql_order_file:
                test_command.extend(['-f', sql_order_file])
            else:
                test_query, test_vars = self.test_query(test_name=test_name)
                test_command.extend(['-c', self.test_cursor.mogrify(test_query, test_vars)])
            env = os.environ.copy()
            env['PGPASSWORD'] = self.user_password
            proc = subprocess.run(oracle_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                                  shell=False, env=env, universal_newlines=True)
            output_open.write(proc.stdout)
            proc = subprocess.run(test_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=False,
                                  env=env, universal_newlines=True)
            output_open.write(proc.stdout)
        output_open.write('\n')

    def print_file_error(self, output_open, data_name, test_name, feedback):
        self.print_file_header(output_open=output_open, data_name=data_name, test_name=test_name, status='error',
                               feedback=feedback)
        output_open.write('\n')

    def run(self):

        try:
            with open(self.output_filename, 'w') as output_open:
                self.init_db()
                for sql_file in sorted(self.specs.keys()):
                    test_name, test_ext = os.path.splitext(sql_file)
                    for data_file, points_total in sorted(self.specs[sql_file].items()):
                        data_name = os.path.splitext(data_file)[0]
                        test_data_name = '{} + {}'.format(test_name, data_name)
                        # check that the submission exists
                        if not os.path.isfile(sql_file):
                            msg = self.ERROR_MSGS['no_submission'].format(sql_file)
                            MarkusUtilsMixin.print_test_error(name=test_data_name, message=msg,
                                                              points_total=points_total)
                            self.print_file_error(output_open=output_open, data_name=data_name, test_name=test_name,
                                                  feedback=msg)
                            continue
                        # check if ordering is required
                        sql_order_file = None
                        order_by = self.order_bys.get(sql_file)
                        if order_by:
                            sql_order_file = '{}_order{}'.format(test_name, test_ext)
                            if not os.path.isfile(sql_order_file):
                                msg = self.ERROR_MSGS['no_submission_order'].format(sql_order_file)
                                MarkusUtilsMixin.print_test_error(name=test_data_name, message=msg,
                                                                  points_total=points_total)
                                self.print_file_error(output_open=output_open, data_name=data_name, test_name=test_name,
                                                      feedback=msg)
                                continue
                        # drop and recreate test schema + dataset, then fetch and compare results
                        try:
                            self.set_test_schema(data_file=data_file)
                            test_results = self.get_test_results(test_name=test_name, sql_file=sql_file,
                                                                 sql_order_file=sql_order_file)
                            oracle_results = self.get_oracle_results(data_name=data_name, test_name=test_name,
                                                                     order_by=order_by)
                            order_on = True if order_by else False
                            output, status = self.check_results(oracle_results=oracle_results,
                                                                test_results=test_results, order_on=order_on)
                            points_awarded = points_total if status == 'pass' else 0
                            MarkusUtilsMixin.print_test_result(name=test_data_name, status=status, output=output,
                                                               points_awarded=points_awarded, points_total=points_total)
                            self.print_file_psql(output_open=output_open, data_name=data_name, test_name=test_name,
                                                 status=status, feedback=output, order_by=order_by,
                                                 sql_order_file=sql_order_file)
                        except Exception as e:
                            self.oracle_connection.commit()
                            self.test_connection.commit()
                            MarkusUtilsMixin.print_test_error(name=test_data_name, message=str(e),
                                                              points_total=points_total)
                            self.print_file_error(output_open=output_open, data_name=data_name, test_name=test_name,
                                                  feedback=str(e))
        except Exception as e:
            MarkusUtilsMixin.print_test_error(name='All SQL tests', message=str(e))
        finally:
            self.close_db()
