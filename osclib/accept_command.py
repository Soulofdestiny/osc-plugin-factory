import re
import urllib2
import warnings
from xml.etree import cElementTree as ET

from osc.core import change_request_state
from osc.core import http_GET, http_PUT, http_DELETE, http_POST
from osc.core import delete_package
from datetime import date


class AcceptCommand(object):
    def __init__(self, api):
        self.api = api

    def find_new_requests(self, project):
        query = "match=state/@name='new'+and+(action/target/@project='{}'+and+action/@type='submit')".format(project)
        url = self.api.makeurl(['search', 'request'], query)

        f = http_GET(url)
        root = ET.parse(f).getroot()

        rqs = []
        for rq in root.findall('request'):
            pkgs = []
            actions = rq.findall('action')
            for action in actions:
                targets = action.findall('target')
                for t in targets:
                    pkgs.append(str(t.get('package')))

            rqs.append({'id': int(rq.get('id')), 'packages': pkgs})
        return rqs

    def reset_rebuild_data(self, project):
        url = self.api.makeurl(['source', self.api.cstaging, 'dashboard', 'support_pkg_rebuild?expand=1'])
        try:
            data = http_GET(url)
        except urllib2.HTTPError:
            return
        tree = ET.parse(data)
        root = tree.getroot()
        for stg in root.findall('staging'):
            if stg.get('name') == project:
                stg.find('rebuild').text = 'unknown'
                stg.find('supportpkg').text = ''

        # reset accpted staging project rebuild state to unknown and clean up
        # supportpkg list
        url = self.api.makeurl(['source', self.api.cstaging, 'dashboard', 'support_pkg_rebuild'])
        content = ET.tostring(root)
        http_PUT(url + '?comment=accept+command+update', data=content)

    def perform(self, project, force=False):
        """Accept the staging project for review and submit to Factory /
        openSUSE 13.2 ...

        Then disable the build to disabled
        :param project: staging project we are working with

        """

        status = self.api.check_project_status(project)

        if not status:
            print('The project "{}" is not yet acceptable.'.format(project))
            if not force:
                return False

        meta = self.api.get_prj_pseudometa(project)
        packages = []
        for req in meta['requests']:
            self.api.rm_from_prj(project, request_id=req['id'], msg='ready to accept')
            packages.append(req['package'])
            msg = 'Accepting staging review for {}'.format(req['package'])
            print(msg)

            oldspecs = self.api.get_filelist_for_package(pkgname=req['package'],
                                                         project=self.api.project,
                                                         extension='spec')
            change_request_state(self.api.apiurl,
                                 str(req['id']),
                                 'accepted',
                                 message='Accept to %s' % self.api.project)
            self.create_new_links(self.api.project, req['package'], oldspecs)

        self.api.accept_status_comment(project, packages)
        self.api.staging_deactivate(project)

        return True

    def cleanup(self, project):
        if not self.api.item_exists(project):
            return False

        pkglist = self.api.list_packages(project)
        clean_list = set(pkglist) - set(self.api.cstaging_nocleanup)

        for package in clean_list:
            print "[cleanup] deleted %s/%s" % (project, package)
            delete_package(self.api.apiurl, project, package, force=True, msg="autocleanup")

        # wipe Test-DVD binaries and breaks kiwi build
        if project.startswith('openSUSE:'):
            for package in pkglist:
                if package.startswith('Test-DVD-'):
                    # intend to break the kiwi file
                    arch = package.split('-')[-1]
                    fakepkgname = 'I-am-breaks-kiwi-build'
                    oldkiwifile = self.api.load_file_content(project, package, 'PRODUCT-'+arch+'.kiwi')
                    if oldkiwifile is not None:
                        newkiwifile = re.sub(r'<repopackage name="openSUSE-release"/>', '<repopackage name="%s"/>' % fakepkgname, oldkiwifile)
                        self.api.save_file_content(project, package, 'PRODUCT-' + arch + '.kiwi', newkiwifile)

                    # do wipe binary now
                    query = { 'cmd': 'wipe' }
                    query['package'] = package
                    query['repository'] = 'images'

                    url = self.api.makeurl(['build', project], query)
                    try:
                        http_POST(url)
                    except urllib2.HTTPError, err:
                        # failed to wipe isos but we can just continue
                        pass

        return True

    def accept_other_new(self):
        changed = False
        rqlist = self.find_new_requests(self.api.project)
        if self.api.cnonfree:
            rqlist += self.find_new_requests(self.api.cnonfree)

        for req in rqlist:
            oldspecs = self.api.get_filelist_for_package(pkgname=req['packages'][0], project=self.api.project, extension='spec')
            print 'Accepting request %d: %s' % (req['id'], ','.join(req['packages']))
            change_request_state(self.api.apiurl, str(req['id']), 'accepted', message='Accept to %s' % self.api.project)
            # Check if all .spec files of the package we just accepted has a package container to build
            self.create_new_links(self.api.project, req['packages'][0], oldspecs)
            changed = True

        return changed

    def create_new_links(self, project, pkgname, oldspeclist):
        filelist = self.api.get_filelist_for_package(pkgname=pkgname, project=project, extension='spec')
        removedspecs = set(oldspeclist) - set(filelist)
        for spec in removedspecs:
            # Deleting all the packages that no longer have a .spec file
            url = self.api.makeurl(['source', project, spec[:-5]])
            print "Deleting package %s from project %s" % (spec[:-5], project)
            try:
                http_DELETE(url)
            except urllib2.HTTPError, err:
                if err.code == 404:
                    # the package link was not yet created, which was likely a mistake from earlier
                    pass
                else:
                    # If the package was there bug could not be delete, raise the error
                    raise
        if len(filelist) > 1:
            # There is more than one .spec file in the package; link package containers as needed
            origmeta = self.api.load_file_content(project, pkgname, '_meta')
            for specfile in filelist:
                package = specfile[:-5]  # stripping .spec off the filename gives the packagename
                if package == pkgname:
                    # This is the original package and does not need to be linked to itself
                    continue
                # Check if the target package already exists, if it does not, we get a HTTP error 404 to catch
                if not self.api.item_exists(project, package):
                    print "Creating new package %s linked to %s" % (package, pkgname)
                    # new package does not exist. Let's link it with new metadata
                    newmeta = re.sub(r'(<package.*name=.){}'.format(pkgname),
                                     r'\1{}'.format(package),
                                     origmeta)
                    newmeta = re.sub(r'<devel.*>',
                                     r'<devel package="{}"/>'.format(pkgname),
                                     newmeta)
                    newmeta = re.sub(r'<bcntsynctag>.*</bcntsynctag>',
                                     r'',
                                     newmeta)
                    newmeta = re.sub(r'</package>',
                                     r'<bcntsynctag>{}</bcntsynctag></package>'.format(pkgname),
                                     newmeta)
                    self.api.save_file_content(project, package, '_meta', newmeta)
                    link = "<link package=\"{}\" cicount=\"copy\" />".format(pkgname)
                    self.api.save_file_content(project, package, '_link', link)
        return True

    def update_factory_version(self):
        """Update project (Factory, 13.2, ...) version if is necessary."""

        # XXX TODO - This method have `factory` in the name.  Can be
        # missleading.

        project = self.api.project
        curr_version = date.today().strftime('%Y%m%d')
        url = self.api.makeurl(['source', project], {'view': 'productlist'})

        products = ET.parse(http_GET(url)).getroot()
        for product in products.findall('product'):
            product_name = product.get('name') + '.product'
            product_pkg = product.get('originpackage')
            url = self.api.makeurl(['source', project, product_pkg,  product_name])
            product_spec = http_GET(url).read()
            new_product = re.sub(r'<version>\d{8}</version>', '<version>%s</version>' % curr_version, product_spec)

            if product_spec != new_product:
                http_PUT(url + '?comment=Update+version', data=new_product)

        service = {'cmd': 'runservice'}

        ports_prjs = ['PowerPC', 'ARM', 'zSystems' ]

        for ports in ports_prjs:
            project = self.api.project + ':' + ports
            if self.api.item_exists(project):
                baseurl = ['source', project, '_product']
                url = self.api.makeurl(baseurl, query=service)
                self.api.retried_POST(url)

    def sync_buildfailures(self):
        """
        Trigger rebuild of packages that failed build in either
        openSUSE:Factory or openSUSE:Factory:Rebuild, but not the
        other Helps over the fact that openSUSE:Factory uses
        rebuild=local, thus sometimes 'hiding' build failures.
        """

        for arch in ["x86_64", "i586"]:
            fact_result = self.api.get_prj_results(self.api.project, arch)
            fact_result = self.api.check_pkgs(fact_result)
            rebuild_result = self.api.get_prj_results(self.api.crebuild, arch)
            rebuild_result = self.api.check_pkgs(rebuild_result)
            result = set(rebuild_result) ^ set(fact_result)

            print sorted(result)

            for package in result:
                self.api.rebuild_pkg(package, self.api.project, arch, None)
                self.api.rebuild_pkg(package, self.api.crebuild, arch, None)
