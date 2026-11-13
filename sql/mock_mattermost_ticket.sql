-- A Mattermost test ticket in the same shape as your ReportedIssues rows.
-- Based on the "Alice" member-count case. Names are deliberately included
-- so you can see the scrubber replace them.
insert into public."ReportedIssues"
  (customer_id, title, issue_description, impact, status, severity, assigned_to,
   app_build_details, identifier_logs, tenant_setup, affected_setup, impact_timing, reproduction_details)
values (
  10077,
  'Wrong number of people in channel',
  'The little people icon at the top of our Engineering channel says 3 but there are definitely 4 of us in there (me, Bob, Carol and the admin). When I click it the list shows everyone. It''s wrong in other channels too, always one short I think. Not urgent but confusing for new people. - Alice (alice@contoso.com)',
  'Confusing for new team members; no data loss.',
  'New', 'Low', 'Alex Johnson',
  '{"app":"Mattermost","version":"10.5.0","platform":"Web","build_number":"","update_channel":"Stable","platform_version":"Chrome"}',
  '{"timestamps":["2026-11-12T10:00:00Z"],"session_ids":[],"correlation_ids":[],"diagnostic_notes":"None attached"}',
  '{"cloud":"Self-hosted","region":"North America","licenses":["Team Edition"],"policies":[],"enabled_addins":[]}',
  '{"file_types":[],"user_roles":["System Admin","Member"],"permissions":["Channel member"],"mailbox_types":[],"affected_users":4}',
  '{"started_at":null,"last_working_at":null,"affected_user_count":4,"last_working_version":null}',
  '{"steps":["Open the Engineering channel","Look at the member icon in the channel header","Click it and count the people in the list"],"actual":"Icon says 3, list shows 4","expected":"Icon says 4","screenshots_attached":false}'
);
