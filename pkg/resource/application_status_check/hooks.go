// Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License"). You may
// not use this file except in compliance with the License. A copy of the
// License is located at
//
//     http://aws.amazon.com/apache2.0/
//
// or in the "license" file accompanying this file. This file is distributed
// on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
// express or implied. See the License for the specific language governing
// permissions and limitations under the License.

package application_status_check

import (
	svcsdk "github.com/aws/aws-sdk-go-v2/service/ec2"
	svcsdktypes "github.com/aws/aws-sdk-go-v2/service/ec2/types"

	"github.com/aws-controllers-k8s/ec2-controller/pkg/tags"
)

// updateTagSpecificationsInCreateRequest adds Tags defined in the Spec to
// CreateApplicationStatusCheckInput.TagSpecifications and ensures the
// ResourceType is always set to 'application-status-check'
func updateTagSpecificationsInCreateRequest(r *resource,
	input *svcsdk.CreateApplicationStatusCheckInput) {
	input.TagSpecifications = nil
	if r.ko.Spec.Tags == nil {
		return
	}
	requestedTags := []svcsdktypes.Tag{}
	for _, desiredTag := range r.ko.Spec.Tags {
		if desiredTag == nil {
			continue
		}
		requestedTags = append(requestedTags, svcsdktypes.Tag{
			Key:   desiredTag.Key,
			Value: desiredTag.Value,
		})
	}
	input.TagSpecifications = []svcsdktypes.TagSpecification{{
		ResourceType: svcsdktypes.ResourceTypeApplicationStatusCheck,
		Tags:         requestedTags,
	}}
}

// checkForMissingRequiredFields prevents DescribeApplicationStatusChecks
// from being called without an ID, which would list every check in the
// account and cause an unrelated check to be adopted.
func (rm *resourceManager) checkForMissingRequiredFields(r *resource) bool {
	return r.ko.Status.ID == nil
}

var syncTags = tags.Sync

// restoreHealthCheckPathRefs copies the Kubernetes-only reference fields
// (SubnetRef, SecurityGroupRef) of Spec.HealthCheckPaths from src onto dst.
//
// The generated output setters rebuild Spec.HealthCheckPaths from the AWS
// response, which never carries the *Ref fields. Without this, the CR's
// references would be replaced by raw IDs after create, and a re-applied
// manifest would produce a delta (non-nil Ref vs nil) on every reconcile.
//
// Paths and destinations are matched by index, the same way the generated
// reference resolution addresses them. A Ref is only restored when the ID it
// resolved to in src equals the ID AWS reports, so real drift in the IDs is
// never masked.
func restoreHealthCheckPathRefs(src, dst *resource) {
	if src == nil || dst == nil {
		return
	}
	srcPaths := src.ko.Spec.HealthCheckPaths
	dstPaths := dst.ko.Spec.HealthCheckPaths
	for i := range dstPaths {
		if i >= len(srcPaths) {
			return
		}
		s, d := srcPaths[i], dstPaths[i]
		if s == nil || d == nil {
			continue
		}
		if s.Source != nil && d.Source != nil {
			if s.Source.SubnetRef != nil && equalStrPtr(s.Source.SubnetID, d.Source.SubnetID) {
				d.Source.SubnetRef = s.Source.SubnetRef.DeepCopy()
			}
			if s.Source.SecurityGroupRef != nil && equalStrPtr(s.Source.SecurityGroupID, d.Source.SecurityGroupID) {
				d.Source.SecurityGroupRef = s.Source.SecurityGroupRef.DeepCopy()
			}
		}
		for j := range d.Destinations {
			if j >= len(s.Destinations) {
				break
			}
			sd, dd := s.Destinations[j], d.Destinations[j]
			if sd == nil || dd == nil {
				continue
			}
			if sd.SubnetRef != nil && equalStrPtr(sd.SubnetID, dd.SubnetID) {
				dd.SubnetRef = sd.SubnetRef.DeepCopy()
			}
			if sd.SecurityGroupRef != nil && equalStrPtr(sd.SecurityGroupID, dd.SecurityGroupID) {
				dd.SecurityGroupRef = sd.SecurityGroupRef.DeepCopy()
			}
		}
	}
}

// equalStrPtr returns true if both pointers are nil or both point to equal
// strings.
func equalStrPtr(a, b *string) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}
