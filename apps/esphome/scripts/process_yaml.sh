#!/bin/bash

replace_includes() {
  local file=$1
  while IFS= read -r line; do
    if [[ $line =~ !include ]]; then
      include_file=$(echo "$line" | awk '{print $3}')
      include_file=${include_file#./}
      if [[ -f "$source_dir/$include_file" ]]; then
        sed '/^---$/d' "$source_dir/$include_file"
      else
        echo "File $source_dir/$include_file not found"
      fi
    else
      if [[ $line != packages:  ]]; then
        echo "$line"
      fi
    fi
  done < "$file"
}

if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Usage: $0 <source_directory> <target_directory>"
  exit 1
fi

source_dir=$1
target_dir=$2

for file in "$source_dir"/*.yaml; do
  replace_includes "$file" > "$target_dir/$(basename "$file")"
done
