"""Exact-target FTP retirement through the existing Actions/OIDC delivery path."""
import json
import os
from pathlib import Path
import subprocess

ACCOUNT = '759775734231'
REGION = 'us-east-2'
STACK = 'ftp-server-stack'
INSTANCE = 'i-06ccfed582e8cdee5'
VOLUME = 'vol-0e4bbd0fd23c6b99b'
FILESYSTEM = 'fs-0af20b248b1bad62e'
BUCKET = 'kill-snap'
DOMAIN = 'cu.jeremy.ninja.'
ZONE = 'Z07996671GFOF4EO145M3'
PUBLIC_IP = '3.137.217.92'
PARAMETER_KEYS = {'InstanceType', 'S3BucketName', 'FTPUser', 'FTPPassword', 'KeyName',
                  'DomainName', 'HostedZoneId', 'SNSTopicArn', 'ServerEnabled'}


def command(*args):
    return subprocess.run(['aws', *args, '--region', REGION, '--output', 'json', '--no-cli-pager'],
                          text=True, capture_output=True, check=False)


def aws(*args):
    result = command(*args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def previous_parameters(keys):
    if not set(keys) <= PARAMETER_KEYS or 'FTPPassword' not in keys:
        raise ValueError('Unexpected stack parameters')
    return [{'ParameterKey': key, 'UsePreviousValue': True} for key in keys if key != 'ServerEnabled'] + [
        {'ParameterKey': 'ServerEnabled', 'ParameterValue': 'false'}]


def validate_changes(change):
    if change.get('NextToken') or len(change.get('Changes', [])) != 1:
        raise ValueError('Expected exactly one resource removal')
    resource = change['Changes'][0]['ResourceChange']
    if (resource.get('LogicalResourceId'), resource.get('ResourceType'),
            resource.get('PhysicalResourceId'), resource.get('Action')) != (
            'FTPServer', 'AWS::EC2::Instance', INSTANCE, 'Remove'):
        raise ValueError('Only the approved FTP instance may be removed')


def dns_removal(records):
    if not records:
        return None
    if (len(records) != 1 or records[0].get('Name') != DOMAIN
            or records[0].get('Type') != 'A'
            or records[0].get('ResourceRecords') != [{'Value': PUBLIC_IP}]
            or 'SetIdentifier' in records[0]):
        raise ValueError('DNS ownership differs from the approved FTP record')
    return {'Changes': [{'Action': 'DELETE', 'ResourceRecordSet': records[0]}]}


def resources():
    rows = aws('cloudformation', 'list-stack-resources', '--stack-name', STACK)['StackResourceSummaries']
    return {row['LogicalResourceId']: row['PhysicalResourceId'] for row in rows
            if row['ResourceStatus'] != 'DELETE_COMPLETE'}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != 'JeremyProffittOrg/ec2-ftp-s3'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise ValueError('Protected manual main workflow required')
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS account')
    # Query parameter names only. Stored password values are never retrieved.
    stack = aws('cloudformation', 'describe-stacks', '--stack-name', STACK, '--query',
                'Stacks[0].{Status:StackStatus,Tags:Tags,Keys:Parameters[].ParameterKey}')
    if stack['Status'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise ValueError('FTP stack must be stable')
    if {tag['Key']: tag['Value'] for tag in stack['Tags']}.get('Project') != 'ec2-ftp-s3':
        raise ValueError('Unexpected stack ownership')
    before = resources()
    if before.get('FTPFileSystem') != FILESYSTEM or len(before) not in (14, 15):
        raise ValueError('Unexpected staging filesystem or resource count')
    aws('s3api', 'head-bucket', '--bucket', BUCKET, '--expected-bucket-owner', ACCOUNT)
    records = aws('route53', 'list-resource-record-sets', '--hosted-zone-id', ZONE,
                  '--query', f"ResourceRecordSets[?Name=='{DOMAIN}' && Type=='A']")
    dns_removal(records)  # Fail before changing the stack if ownership differs.
    if 'FTPServer' in before:
        if before['FTPServer'] != INSTANCE:
            raise ValueError('Unexpected FTP instance')
        instance = aws('ec2', 'describe-instances', '--instance-ids', INSTANCE,
                       '--query', 'Reservations[0].Instances[0].{State:State.Name,Disks:BlockDeviceMappings}')
        disks = instance['Disks']
        if (len(disks) != 1 or disks[0].get('Ebs', {}).get('VolumeId') != VOLUME
                or not disks[0]['Ebs'].get('DeleteOnTermination')):
            raise ValueError('Unexpected root disk or deletion policy')
        for output in ('InstanceId', 'PublicIP', 'FTPPort'):
            if aws('cloudformation', 'list-imports', '--export-name', STACK + '-' + output)['Imports']:
                raise ValueError('Another stack imports an FTP endpoint output')
        name = 'retire-ftp-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
        aws('cloudformation', 'create-change-set', '--stack-name', STACK, '--change-set-name', name,
            '--change-set-type', 'UPDATE', '--template-body', 'file://' + str(Path('cloudformation-template.yaml').resolve()),
            '--capabilities', 'CAPABILITY_IAM', '--tags', json.dumps(stack['Tags']),
            '--parameters', json.dumps(previous_parameters(stack['Keys'])))
        aws('cloudformation', 'wait', 'change-set-create-complete', '--stack-name', STACK,
            '--change-set-name', name)
        validate_changes(aws('cloudformation', 'describe-change-set', '--stack-name', STACK,
                             '--change-set-name', name))
        print('Validated exactly one FTPServer removal:', INSTANCE, flush=True)
        aws('cloudformation', 'execute-change-set', '--stack-name', STACK, '--change-set-name', name,
            '--disable-rollback')
        aws('cloudformation', 'wait', 'stack-update-complete', '--stack-name', STACK)
    aws('ec2', 'wait', 'instance-terminated', '--instance-ids', INSTANCE)
    aws('ec2', 'wait', 'volume-deleted', '--volume-ids', VOLUME)
    after = resources()
    if after != {key: value for key, value in before.items() if key != 'FTPServer'}:
        raise ValueError('A retained stack resource changed')
    records = aws('route53', 'list-resource-record-sets', '--hosted-zone-id', ZONE,
                  '--query', f"ResourceRecordSets[?Name=='{DOMAIN}' && Type=='A']")
    removal = dns_removal(records)
    if removal:
        change = aws('route53', 'change-resource-record-sets', '--hosted-zone-id', ZONE,
                     '--change-batch', json.dumps(removal))
        aws('route53', 'wait', 'resource-record-sets-changed', '--id', change['ChangeInfo']['Id'])
    if aws('route53', 'list-resource-record-sets', '--hosted-zone-id', ZONE,
           '--query', f"ResourceRecordSets[?Name=='{DOMAIN}' && Type=='A']"):
        raise ValueError('Old FTP DNS A record remains')
    aws('s3api', 'head-bucket', '--bucket', BUCKET, '--expected-bucket-owner', ACCOUNT)
    evidence = {'account': ACCOUNT, 'instance': INSTANCE, 'state': 'terminated',
                'root_volume': VOLUME, 'root_volume_state': 'deleted', 'old_public_ip': PUBLIC_IP,
                'dns': DOMAIN, 'dns_state': 'removed', 'shared_bucket_preserved': BUCKET,
                'staging_filesystem_preserved': FILESYSTEM, 'retained_stack_resources': after,
                'source_commit': os.environ['GITHUB_SHA'], 'workflow_run': os.environ['GITHUB_RUN_ID'],
                'rebuild_blueprint': 'cloudformation-template.yaml; ServerEnabled defaults false'}
    Path('retirement-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == '__main__':
    main()
