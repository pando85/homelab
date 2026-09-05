#!/bin/bash
set -e

echo "=== Readest Upload Test ==="
echo ""

# Port-forward to rest service
echo "Starting port-forward to readest-rest..."
kubectl --context=grigri port-forward -n readest svc/readest-rest 3000:3000 &
PF_PID=$!
sleep 3

# Cleanup on exit
cleanup() {
  echo "Stopping port-forward..."
  kill $PF_PID 2>/dev/null || true
  wait $PF_PID 2>/dev/null || true
}
trap cleanup EXIT

# Get JWT secret
echo "Getting JWT secret..."
JWT_SECRET=$(kubectl --context=grigri get secret -n readest readest-secrets -o jsonpath='{.data.secret}' | base64 -d)

# Create test JWT for authenticated user
echo "Creating test JWT..."
USER_ID="f50bad5c-87f3-42c7-b600-05ba2024f0aa"
JWT=$(python3 -c "
import jwt
import datetime
payload = {
  'sub': '$USER_ID',
  'role': 'authenticated',
  'aud': 'authenticated',
  'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1),
  'iat': datetime.datetime.utcnow()
}
print(jwt.encode(payload, '$JWT_SECRET', algorithm='HS256'))
")

echo "Testing upload with authenticated role..."
echo ""

# Test 1: Upload file metadata
echo "Test 1: Upload file metadata"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:3000/files" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d "{
    \"user_id\": \"$USER_ID\",
    \"book_hash\": \"test-hash-$(date +%s)\",
    \"file_key\": \"test-$(date +%s)\",
    \"file_size\": 1024
  }")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

echo "HTTP Status: $HTTP_CODE"
echo "Response: $BODY"
echo ""

if [ "$HTTP_CODE" = "201" ]; then
  echo "✅ Upload successful!"
else
  echo "❌ Upload failed"
fi

# Test 2: Get uploaded files
echo "Test 2: Get uploaded files"
RESPONSE=$(curl -s -w "\n%{http_code}" -X GET "http://localhost:3000/files?user_id=eq.$USER_ID&select=id,file_key,file_size&order=created_at.desc&limit=1" \
  -H "Authorization: Bearer $JWT" \
  -H "Accept: application/json")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

echo "HTTP Status: $HTTP_CODE"
echo "Response: $BODY"
echo ""

if [ "$HTTP_CODE" = "200" ]; then
  echo "✅ Read successful!"
else
  echo "❌ Read failed"
fi

# Test 3: Try upload with anon role (should fail)
echo "Test 3: Upload with anon role (should fail)"
ANON_JWT=$(python3 -c "
import jwt
import datetime
payload = {
  'role': 'anon',
  'aud': 'authenticated',
  'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1),
  'iat': datetime.datetime.utcnow()
}
print(jwt.encode(payload, '$JWT_SECRET', algorithm='HS256'))
")

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:3000/files" \
  -H "Authorization: Bearer $ANON_JWT" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d "{
    \"user_id\": \"$USER_ID\",
    \"book_hash\": \"anon-test-hash-$(date +%s)\",
    \"file_key\": \"anon-test-$(date +%s)\",
    \"file_size\": 1024
  }")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

echo "HTTP Status: $HTTP_CODE"
echo "Response: $BODY"
echo ""

if [ "$HTTP_CODE" = "401" ]; then
  echo "✅ Anon upload correctly rejected by RLS"
else
  echo "❌ Anon upload should have failed"
fi

echo ""
echo "=== Test Complete ==="
