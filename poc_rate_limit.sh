#!/bin/bash
echo "Sending 35 requests to /generate-sql rapidly..."
for i in {1..35}; do
  status=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8080/generate-sql -H "Content-Type: application/json" -d '{"query": "test"}')
  echo "Request $i: HTTP $status"
done
