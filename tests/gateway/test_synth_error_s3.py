"""A synth-authored S3 error must be readable by the SDK that asked for it.

`_respond` has carried an `s3` branch since the gateway's first release;
`synth_error` -- the path a synth MODEL uses to author its own error, with the
exact wire code -- did not. So an S3 error written by a model fell through to
the AWS-JSON body, which botocore parses with RestXMLParser. The caller does not
see odin's error; it sees a parse failure, which is the least actionable outcome
of all.

Found while building S3 bucket notifications, before anything shipped.
"""
import boto3
from botocore.parsers import RestXMLParser

from odin.gateway import errors


def test_a_synth_authored_s3_error_parses_as_an_s3_error():
    response = errors.synth_error("s3", "InvalidArgument", "Filter rule is not valid", 400)

    assert response.media_type == "application/xml", response.media_type
    parsed = RestXMLParser().parse(
        {"status_code": response.status_code, "headers": {}, "body": bytes(response.body)},
        boto3.client("s3", region_name="us-east-1",
                     aws_access_key_id="x", aws_secret_access_key="y").meta.service_model.operation_model(
            "PutBucketNotificationConfiguration").output_shape,
    )
    assert parsed["Error"]["Code"] == "InvalidArgument", parsed
    assert parsed["Error"]["Message"] == "Filter rule is not valid", parsed
