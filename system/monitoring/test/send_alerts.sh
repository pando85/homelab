#!/usr/bin/env bash

# Constants
ALERT_URL='https://alertmanager.internal.grigri.cloud/api/v2/alerts'
BOLD=$(tput bold)
NORMAL=$(tput sgr0)

# Function to generate a random alert name
generate_alert_name() {
  echo "fooAlert-$RANDOM"
}

# Function to generate JSON payload for the alert
generate_post_data() {
  local status=$1
  local alert_name=$2
  local extra_fields=$3

  cat <<EOF
[
  {
    "status": "$status",
    "labels": {
      "alertname": "${alert_name}",
      "service": "my-service",
      "severity": "warning",
      "instance": "${alert_name}.example.net",
      "namespace": "fake-foo",
      "label_costcentre": "FOO"
    },
    "annotations": {
      "summary": "High latency is high!",
      "description": "Description example",
      "message": "Message example",
      "runbook_url": "https://example.com/${alert_name}"
    },
    "generatorURL": "https://example.com/${alert_name}"
    ${extra_fields}
  },
  {
    "status": "$status",
    "labels": {
      "alertname": "${alert_name}2",
      "service": "my-service",
      "severity": "critical",
      "instance": "${alert_name}2.example.com",
      "namespace": "fake-boo",
      "label_costcentre": "FOO"
    },
    "annotations": {
      "summary": "2High latency is high!",
      "description": "2Description example",
      "message": "2Message example",
      "runbook_url": "https://example.com/${alert_name}"
    },
    "generatorURL": "https://example.com/${alert_name}"
    ${extra_fields}
  }
]
EOF
}

# Function to get the current timestamp in RFC3339 format
get_current_timestamp() {
  date --rfc-3339=seconds | sed 's/ /T/'
}

# Main script execution
main() {
  local alert_name
  alert_name=$(generate_alert_name)

  echo "${BOLD}Firing alert ${alert_name}${NORMAL}"
  local starts_at
  starts_at=$(get_current_timestamp)
  local post_data
  post_data=$(generate_post_data 'firing' "${alert_name}" ",\"startsAt\": \"${starts_at}\"")
  curl -s -XPOST "${ALERT_URL}" -H "Content-Type: application/json" --data "${post_data}" --trace-ascii /dev/stdout
  echo -e "\n"

  echo "${BOLD}Press enter to resolve alert ${alert_name}${NORMAL}"
  read -r

  echo "${BOLD}Sending resolved${NORMAL}"
  local ends_at
  ends_at=$(get_current_timestamp)
  post_data=$(generate_post_data 'resolved' "${alert_name}" ",\"startsAt\": \"${starts_at}\", \"endsAt\": \"${ends_at}\"")
  curl -s -XPOST "${ALERT_URL}" -H "Content-Type: application/json" --data "${post_data}" --trace-ascii /dev/stdout
  echo -e "\n"
}

# Invoke main function
main
