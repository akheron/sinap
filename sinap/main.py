from argparse import ArgumentParser
from tornado.ioloop import IOLoop
from sinap.bot import Bot


def main():
    parser = ArgumentParser()
    parser.add_argument(
        '-c', '--config', default='config.yml',
        help='Configuration file [default: config.yml]',
    )
    args = parser.parse_args()

    bot = Bot(args.config)
    bot.run()

    IOLoop.instance().start()


if __name__ == '__main__':
    main()
