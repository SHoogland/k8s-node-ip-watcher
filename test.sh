export CLIENT_ID=xxx
export CLIENT_SECRET=xxx
export PROJECT_ID=xxx

ACCESS_TOKEN=$(curl -fsS --request POST https://cloud.mongodb.com/api/oauth/token \
  --header "Authorization: Basic $(printf '%s' "${CLIENT_ID}:${CLIENT_SECRET}" | base64 | tr -d '\n')" \
  --header "Content-Type: application/x-www-form-urlencoded" \
  --header "Accept: application/json" \
  --data "grant_type=client_credentials" | jq -r '.access_token')

curl --header "Authorization: Bearer ${ACCESS_TOKEN}" \
  --header "Accept: application/vnd.atlas.2025-03-12+json" \
  -X GET "https://cloud.mongodb.com/api/atlas/v2/orgs?pretty=true"

curl -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Accept: application/vnd.atlas.2025-03-12+json" \
  "https://cloud.mongodb.com/api/atlas/v2/groups"

# Test allowlist fetch
curl -s -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Accept: application/vnd.atlas.2025-03-12+json" \
  "https://cloud.mongodb.com/api/atlas/v2/groups/$PROJECT_ID/accessList" | jq .
