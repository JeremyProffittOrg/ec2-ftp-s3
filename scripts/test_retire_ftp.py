import copy
import unittest
from unittest.mock import patch
import retire_ftp as retire


class RetirementTests(unittest.TestCase):
    def test_list_imports_accepts_only_the_specific_no_consumers_error(self):
        name = retire.STACK + '-InstanceId'
        result = retire.subprocess.CompletedProcess([], 255, '',
            f"(ValidationError) when calling the ListImports operation: Export '{name}' is not imported by any stack.")
        with patch.object(retire, 'command', return_value=result):
            self.assertEqual(retire.export_imports(name), [])
        result.stderr = 'An error occurred (AccessDenied) when calling ListImports'
        with patch.object(retire, 'command', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'AccessDenied'):
                retire.export_imports(name)
        result = retire.subprocess.CompletedProcess([], 0, '{"Imports":["another-stack"]}', '')
        with patch.object(retire, 'command', return_value=result):
            self.assertEqual(retire.export_imports(name), ['another-stack'])

    def test_exact_removal_and_rejection_of_other_resource_mutations(self):
        change = {'Changes': [{'ResourceChange': {'LogicalResourceId': 'FTPServer',
                  'ResourceType': 'AWS::EC2::Instance', 'PhysicalResourceId': retire.INSTANCE,
                  'Action': 'Remove'}}]}
        retire.validate_changes(change)
        for key, value in [('Action', 'Add'), ('Action', 'Modify'),
                           ('LogicalResourceId', 'FTPFileSystem'), ('PhysicalResourceId', 'other')]:
            bad = copy.deepcopy(change)
            bad['Changes'][0]['ResourceChange'][key] = value
            with self.assertRaises(ValueError):
                retire.validate_changes(bad)
        with self.assertRaises(ValueError):
            retire.validate_changes({'Changes': change['Changes'] * 2})

    def test_password_reused_without_a_value_and_server_always_disabled(self):
        params = retire.previous_parameters(['FTPPassword', 'S3BucketName', 'ServerEnabled'])
        self.assertEqual(params, [{'ParameterKey': 'FTPPassword', 'UsePreviousValue': True},
                                 {'ParameterKey': 'S3BucketName', 'UsePreviousValue': True},
                                 {'ParameterKey': 'ServerEnabled', 'ParameterValue': 'false'}])
        with self.assertRaises(ValueError):
            retire.previous_parameters(['UnexpectedSecret'])

    def test_dns_delete_only_for_the_exact_old_ftp_address(self):
        record = {'Name': retire.DOMAIN, 'Type': 'A', 'TTL': 300,
                  'ResourceRecords': [{'Value': retire.PUBLIC_IP}]}
        self.assertEqual(retire.dns_removal([record])['Changes'][0]['Action'], 'DELETE')
        self.assertIsNone(retire.dns_removal([]))
        for other in ('203.0.113.1', '203.0.113.2'):
            changed = copy.deepcopy(record)
            changed['ResourceRecords'].append({'Value': other})
            with self.assertRaises(ValueError):
                retire.dns_removal([changed])

    def test_local_execution_rejected_before_any_aws_call(self):
        with patch.dict(retire.os.environ, {}, clear=True), patch.object(retire, 'aws') as aws:
            with self.assertRaisesRegex(ValueError, 'Protected manual main workflow'):
                retire.main()
            aws.assert_not_called()


if __name__ == '__main__':
    unittest.main()
