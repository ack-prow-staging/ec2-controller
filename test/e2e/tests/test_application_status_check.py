# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Integration tests for the ApplicationStatusCheck API.
"""

import logging
import time

import pytest

from acktest import tags
from acktest.k8s import resource as k8s
from acktest.resources import random_suffix_name
from e2e import service_marker, CRD_GROUP, CRD_VERSION, load_ec2_resource
from e2e.bootstrap_resources import get_bootstrap_resources
from e2e.replacement_values import REPLACEMENT_VALUES
from e2e.tests.helper import EC2Validator

RESOURCE_PLURAL = "applicationstatuschecks"
SUBNET_PLURAL = "subnets"
SECURITY_GROUP_PLURAL = "securitygroups"

CREATE_WAIT_AFTER_SECONDS = 10
MODIFY_WAIT_AFTER_SECONDS = 10
DELETE_WAIT_AFTER_SECONDS = 10
WAIT_PERIODS = 10

# Must be inside SharedTestVPC and not overlap subnets used by other tests
# (test_subnet.py uses 10.0.255.0/24).
REF_SUBNET_CIDR = "10.0.254.0/24"


def _create_cr(plural, name, resource_file, replacements):
    resource_data = load_ec2_resource(
        resource_file,
        additional_replacements=replacements,
    )
    logging.debug(resource_data)
    ref = k8s.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, plural, name, namespace="default",
    )
    k8s.create_custom_resource(ref, resource_data)
    time.sleep(CREATE_WAIT_AFTER_SECONDS)
    cr = k8s.wait_resource_consumed_by_controller(ref)
    assert cr is not None
    assert k8s.get_resource_exists(ref)
    return ref, cr


def _delete_cr(ref):
    try:
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted
    except:
        pass


@pytest.fixture
def simple_application_status_check():
    resource_name = random_suffix_name("asc-test", 24)

    replacements = REPLACEMENT_VALUES.copy()
    replacements["ASC_NAME"] = resource_name
    replacements["PROTOCOL"] = "http"
    replacements["PORT"] = "80"
    replacements["HEALTH_CHECK_PATH"] = "/health"
    replacements["TAG_KEY"] = "initialtagkey"
    replacements["TAG_VALUE"] = "initialtagvalue"

    ref, cr = _create_cr(
        RESOURCE_PLURAL, resource_name, "application_status_check", replacements,
    )

    yield (ref, cr)

    _delete_cr(ref)


@pytest.fixture
def application_status_check_with_refs():
    test_vpc = get_bootstrap_resources().SharedTestVPC

    subnet_name = random_suffix_name("asc-ref-subnet", 24)
    subnet_replacements = REPLACEMENT_VALUES.copy()
    subnet_replacements["SUBNET_NAME"] = subnet_name
    subnet_replacements["CIDR_BLOCK"] = REF_SUBNET_CIDR
    subnet_replacements["VPC_ID"] = test_vpc.vpc_id
    subnet_replacements["TAG_KEY"] = "test-key"
    subnet_replacements["TAG_VALUE"] = "test-value"
    subnet_ref, _ = _create_cr(
        SUBNET_PLURAL, subnet_name, "subnet", subnet_replacements,
    )

    sg_name = random_suffix_name("asc-ref-sg", 24)
    sg_replacements = REPLACEMENT_VALUES.copy()
    sg_replacements["SECURITY_GROUP_NAME"] = sg_name
    sg_replacements["SECURITY_GROUP_DESCRIPTION"] = "application status check reference test"
    sg_replacements["VPC_ID"] = test_vpc.vpc_id
    sg_replacements["TAG_KEY"] = "test-key"
    sg_replacements["TAG_VALUE"] = "test-value"
    sg_ref, _ = _create_cr(
        SECURITY_GROUP_PLURAL, sg_name, "security_group", sg_replacements,
    )

    assert k8s.wait_on_condition(subnet_ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS)
    assert k8s.wait_on_condition(sg_ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS)

    asc_name = random_suffix_name("asc-ref-test", 24)
    asc_replacements = REPLACEMENT_VALUES.copy()
    asc_replacements["ASC_NAME"] = asc_name
    asc_replacements["SUBNET_NAME"] = subnet_name
    asc_replacements["SECURITY_GROUP_NAME"] = sg_name
    asc_ref, asc_cr = _create_cr(
        RESOURCE_PLURAL, asc_name, "application_status_check_ref", asc_replacements,
    )

    yield (asc_ref, asc_cr, subnet_ref, sg_ref)

    # Delete the check before the resources it references
    _delete_cr(asc_ref)
    time.sleep(DELETE_WAIT_AFTER_SECONDS)
    _delete_cr(sg_ref)
    _delete_cr(subnet_ref)


@service_marker
@pytest.mark.canary
class TestApplicationStatusCheck:
    def test_crud(self, ec2_client, simple_application_status_check):
        (ref, cr) = simple_application_status_check
        ec2_validator = EC2Validator(ec2_client)

        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )

        cr = k8s.get_resource(ref)
        assert "id" in cr["status"]
        asc_id = cr["status"]["id"]
        assert asc_id

        # Check the resource exists in AWS with the requested values
        asc = ec2_validator.get_application_status_check(asc_id)
        assert asc is not None
        assert asc["Protocol"] == "http"
        assert asc["Port"] == 80
        assert asc["Path"] == "/health"

        tags.assert_ack_system_tags(tags=asc["Tags"])
        tags.assert_equal_without_ack_tags(
            expected={"initialtagkey": "initialtagvalue"},
            actual=asc["Tags"],
        )

        # Server-defaulted fields are late-initialized into the Spec and
        # match what AWS reports
        for spec_field, aws_field in [
            ("aggregation", "Aggregation"),
            ("interval", "Interval"),
            ("timeout", "Timeout"),
            ("failureThreshold", "FailureThreshold"),
            ("successThreshold", "SuccessThreshold"),
            ("statusCodeMatcher", "StatusCodeMatcher"),
            ("initializationGracePeriodSeconds", "InitializationGracePeriodSeconds"),
        ]:
            assert spec_field in cr["spec"], f"{spec_field} was not late-initialized"
            assert cr["spec"][spec_field] == asc[aws_field]

        # Update configuration fields
        updates = {
            "spec": {
                "timeout": 10,
                "failureThreshold": 3,
                "path": "/healthz",
            }
        }
        k8s.patch_custom_resource(ref, updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )

        asc = ec2_validator.get_application_status_check(asc_id)
        assert asc is not None
        assert asc["Timeout"] == 10
        assert asc["FailureThreshold"] == 3
        assert asc["Path"] == "/healthz"

        # Tags are not carried by ModifyApplicationStatusCheck; a config-only
        # update must leave them untouched in both the CR and AWS.
        tags.assert_ack_system_tags(tags=asc["Tags"])
        tags.assert_equal_without_ack_tags(
            expected={"initialtagkey": "initialtagvalue"},
            actual=asc["Tags"],
        )

        cr = k8s.get_resource(ref)
        assert cr["spec"]["timeout"] == 10
        assert cr["spec"]["failureThreshold"] == 3
        assert cr["spec"]["path"] == "/healthz"
        cr_tags = {t["key"]: t["value"] for t in cr["spec"].get("tags", [])}
        assert cr_tags.get("initialtagkey") == "initialtagvalue"

        # Update tags: replace the initial tag with a new one
        updated_tags = [{"key": "updatedtagkey", "value": "updatedtagvalue"}]
        k8s.patch_custom_resource(ref, {"spec": {"tags": updated_tags}})
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )

        asc = ec2_validator.get_application_status_check(asc_id)
        assert asc is not None
        tags.assert_ack_system_tags(tags=asc["Tags"])
        tags.assert_equal_without_ack_tags(
            expected={"updatedtagkey": "updatedtagvalue"},
            actual=asc["Tags"],
        )

        # Remove all user tags
        k8s.patch_custom_resource(ref, {"spec": {"tags": []}})
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )

        asc = ec2_validator.get_application_status_check(asc_id)
        assert asc is not None
        tags.assert_ack_system_tags(tags=asc["Tags"])
        tags.assert_equal_without_ack_tags(expected={}, actual=asc["Tags"])

        # Delete
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted is True
        time.sleep(DELETE_WAIT_AFTER_SECONDS)

        # Without IncludeAll, checks in the deletion grace period are not
        # returned, so the check must no longer be visible
        ec2_validator.assert_application_status_check(asc_id, exists=False)

    def test_health_check_paths_with_refs(self, ec2_client, application_status_check_with_refs):
        (ref, cr, subnet_ref, sg_ref) = application_status_check_with_refs
        ec2_validator = EC2Validator(ec2_client)

        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )

        subnet_id = k8s.get_resource(subnet_ref)["status"]["subnetID"]
        sg_id = k8s.get_resource(sg_ref)["status"]["id"]

        cr = k8s.get_resource(ref)
        asc_id = cr["status"]["id"]

        # The user's references must survive the create (the output setters
        # rebuild healthCheckPaths from AWS, which knows nothing about refs).
        self._assert_refs_present(cr, subnet_ref, sg_ref)

        asc = ec2_validator.get_application_status_check(asc_id)
        assert asc is not None
        self._assert_aws_paths(asc, subnet_id, sg_id)

        # Force a reconcile with an unrelated change and confirm the refs are
        # still present, AWS paths are unchanged, and only the patched field
        # was modified.
        k8s.patch_custom_resource(ref, {"spec": {"timeout": 8}})
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )

        cr = k8s.get_resource(ref)
        assert cr["spec"]["timeout"] == 8
        self._assert_refs_present(cr, subnet_ref, sg_ref)

        asc = ec2_validator.get_application_status_check(asc_id)
        assert asc is not None
        assert asc["Timeout"] == 8
        self._assert_aws_paths(asc, subnet_id, sg_id)

        # Once that update has settled, a further reconcile (triggered by a
        # tag-only change, which skips Modify) must not send another Modify:
        # healthCheckPaths/statusCodeMatcher round-trip without a delta.
        modify_time = cr["status"].get("modifyTime")
        k8s.patch_custom_resource(
            ref, {"spec": {"tags": [{"key": "reconcile", "value": "now"}]}},
        )
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, "ACK.ResourceSynced", "True", wait_periods=WAIT_PERIODS,
        )
        cr = k8s.get_resource(ref)
        self._assert_refs_present(cr, subnet_ref, sg_ref)
        assert cr["status"].get("modifyTime") == modify_time

    @staticmethod
    def _assert_refs_present(cr, subnet_ref, sg_ref):
        paths = cr["spec"]["healthCheckPaths"]
        assert len(paths) == 1
        src = paths[0]["source"]
        dst = paths[0]["destinations"][0]
        for obj in (src, dst):
            assert obj["subnetRef"]["from"]["name"] == subnet_ref.name
            assert obj["securityGroupRef"]["from"]["name"] == sg_ref.name

    @staticmethod
    def _assert_aws_paths(asc, subnet_id, sg_id):
        aws_paths = asc["HealthCheckPaths"]
        assert len(aws_paths) == 1
        assert aws_paths[0]["Source"]["SubnetId"] == subnet_id
        assert aws_paths[0]["Source"]["SecurityGroupId"] == sg_id
        assert aws_paths[0]["Destinations"][0]["SubnetId"] == subnet_id
        assert aws_paths[0]["Destinations"][0]["SecurityGroupId"] == sg_id
