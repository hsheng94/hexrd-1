from __future__ import print_function, division, absolute_import


descr = 'Process diffraction data to find grain orientations'
example = """
examples:
    hexrd find-orientations configuration.yml
"""


def configure_parser(sub_parsers):
    p = sub_parsers.add_parser(
        'find-orientations',
        description = descr,
        help = descr
        )
    p.add_argument(
        'yml', type=str,
        help='YAML configuration file'
        )
    p.add_argument(
        '-q', '--quiet', action='store_true',
        help="don't report progress in terminal"
        )
    p.add_argument(
        '-f', '--force', action='store_true',
        help='overwrites existing analysis'
        )
    p.add_argument(
        '--hkls', metavar='HKLs', type=str, default=None,
        help="""\
list hkl entries in the materials file to use for fitting
if None, defaults to list specified in the yml file"""
        )
    p.set_defaults(func=execute)


def execute(args, parser):
    import logging
    import os
    import sys

    import yaml

    from hexrd import config
    from hexrd.findorientations import find_orientations


    # make sure hkls are passed in as a list of ints
    try:
        if args.hkls is not None:
            args.hkls = [int(i) for i in args.hkls.split(',') if i]
    except AttributeError:
        # called from fit-grains, hkls not passed
        args.hkls = None

    # configure logging to the console
    log_level = logging.DEBUG if args.debug else logging.INFO
    if args.quiet:
        log_level = logging.ERROR
    logger = logging.getLogger('hexrd')
    logger.setLevel(log_level)
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(
        logging.Formatter('%(asctime)s - %(message)s', '%y-%m-%d %H:%M:%S')
        )
    logger.addHandler(ch)
    logger.info('=== begin find-orientations ===')

    # load the configuration settings
    cfg = config.open(args.yml)[0]

    # prepare the analysis directory
    quats_f = os.path.join(cfg.working_dir, 'accepted_orientations.dat')
    if os.path.exists(quats_f) and not args.force:
        logger.error(
            '%s already exists. Change yml file or specify "force"', quats_f
            )
        sys.exit()
    if not os.path.exists(cfg.working_dir):
        os.makedirs(cfg.working_dir)

    # configure logging to file
    logfile = os.path.join(
        cfg.working_dir,
        'find-orientations.log'
        )
    fh = logging.FileHandler(logfile, mode='w')
    fh.setLevel(log_level)
    fh.setFormatter(
        logging.Formatter(
            '%(asctime)s - %(name)s - %(message)s',
            '%m-%d %H:%M:%S'
            )
        )
    logger.info("logging to %s", logfile)
    logger.addHandler(fh)

    # process the data
    find_orientations(cfg, hkls=args.hkls)

    # clean up the logging
    fh.flush()
    fh.close()
    logger.removeHandler(fh)
    logger.info('=== end find-orientations ===')
    ch.flush()
    ch.close()
    logger.removeHandler(ch)
