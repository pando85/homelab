# Ansible Playbook Setup Readme

This README provides instructions for deploying using Ansible playbooks. Please follow the steps below for a successful deployment.

## Prerequisites
- Ansible installed on your local machine.
- Access to the required secrets stored in `pass` for deployment.
- Knowledge of how to obtain Telegram `chat_id` and `token`.

## Steps to Deploy

1. **Access the Metal Folder**
    - Navigate to the `metal` folder in your terminal.

2. **Install `pass` on Ubuntu**
    - If `pass` is not already installed on your Ubuntu system, follow these instructions:
        - Open your terminal.
        - Run the following command to install `pass`:
            ```shell
            sudo apt-get install pass
            ```

3. **Set Secrets in `pass`**
    - Use the `pass` command-line tool to set the following secrets:
        - `grigri/telegram/chat_id`
        - `grigri/telegram/token`
        - `k8s.grigri/prepare_password`
        - `grigri/smtp`
    For example:
    ```shell
    pass insert k8s.dabol/telegram/chat_id
    ```

4. **Run Prepare Playbook**
    - Execute the following command to prepare the deployment:
        ```shell
        make prepare
        ```

5. **Run Cluster Playbook**
    - Once the preparation is complete, run the following command to deploy the cluster:
        ```shell
        make cluster
        ```

## Configuration Checks

Before deploying, ensure the following configurations are correctly set up:

- **Check Email Address Configuration**:
    - Verify the email address configuration in the following files:
        - `/metal/roles/setup/templates/sasl_passwd.j2`
        - `bootstrap/argocd/values.yaml`
        - `/metal/roles/setup/tasks/main.yml`
        - `/metal/roles/setup/templates/telegram-notification.j2`

- **Check Certificates**:
    - Ensure the certificates are correctly configured in:
        - `/metal/inventory/group_vars/all/prepare.yml`

- **Check Inventory**:
    - Verify the inventory in:
        - `/metal/inventory/hosts.ini`

- **Check Hosts Matching**:
    - Make sure the hosts are matching in the following playbooks:
        - `/metal/playbooks/install/backups.yml`
        - `/metal/playbooks/install/cluster.yml`
        - `/metal/playbooks/install/prepare.yml`

## About Ansible

Ansible is an open-source automation tool used for configuration management, application deployment, and task automation. It allows you to automate repetitive tasks, making it easier to manage and deploy infrastructure.

## About `pass`

`pass` is a simple command-line password manager for Unix systems. It stores each password in a GPG-encrypted file and allows you to organize them into folders. In this setup, `pass` is used to securely store sensitive information such as API tokens and passwords required for deployment.

## Obtaining Telegram `chat_id` and `token`

To obtain the Telegram `chat_id` and `token`, follow these steps:
- **Create a Telegram Bot**:
    1. Start a conversation with the BotFather on Telegram.
    2. Use the `/newbot` command to create a new bot and follow the instructions.
    3. Once the bot is created, BotFather will provide you with a `token`.
- **Get Chat ID**:
    1. Add your bot to the desired Telegram group or channel.
    2. Send a message to the group/channel.
    3. Use the following API call to get the `chat_id`:
        ```shell
        curl https://api.telegram.org/bot<YOUR_TOKEN_HERE>/getUpdates
        ```
    4. Look for the `chat` object within the response JSON. The `id` field within the `chat` object represents the `chat_id` of the group/channel.

Make sure to keep the `token` and `chat_id` secure and not expose them publicly. These credentials are required for integrating Telegram notifications with your deployment setup.

Feel free to reach out if you have any questions or need further assistance.
