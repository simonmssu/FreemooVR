/* -*- Mode: C++; tab-width: 4; indent-tabs-mode: nil; c-basic-offset: 4 -*- */
#include <OpenThreads/ScopedLock>

#include <osg/PolygonMode>
#include <osg/MatrixTransform>
#include <osg/Projection>
#include <osg/Geometry>
#include <osg/Texture>
#include <osg/TexGen>
#include <osg/Geode>
#include <osg/ShapeDrawable>
#include <osg/PolygonOffset>
#include <osg/CullSettings>
#include <osg/TextureCubeMap>
#include <osg/TexMat>
#include <osg/Light>
#include <osg/LightSource>
#include <osg/PolygonOffset>
#include <osg/CullFace>
#include <osg/Material>
#include <osg/PositionAttitudeTransform>
#include <osg/ArgumentParser>
#include <osg/TextureRectangle>
#include <osg/Texture2D>
#include <osg/Camera>
#include <osg/TexGenNode>
#include <osg/View>
#include <osg/io_utils>
#include <osg/AutoTransform>

#include <osgGA/TrackballManipulator>

#include <osgDB/ReadFile>
#include <osgDB/WriteFile>
#include <osgDB/FileUtils>

#include <osgViewer/ViewerEventHandlers>

#include <stdio.h>
#include <stdexcept>
#include <sstream>
#include <iostream>

#include <ros/ros.h>
#include <ros/time.h>
#include <ros/console.h>
#include "sensor_msgs/CameraInfo.h"
#include "geometry_msgs/Transform.h"

#include <boost/program_options.hpp>

#include <jansson.h>

#include "util.h"
#include "DisplaySurfaceGeometry.h"
#include "camera_model.h"

class MyNode {
public:
    MyNode(int argc, char**argv);
    void setup_viewer(std::string json_config, int& width, int& height);
    int run();
    void gotTfCallback(const geometry_msgs::Transform& msg );
    void gotCameraInfoCallback(const sensor_msgs::CameraInfoConstPtr& msg);
    void set_tf_mode(std::string tf_mode);
private:
    osgViewer::Viewer* _viewer;
	ros::NodeHandle _node_handle;
	ros::Subscriber sub1;
	ros::Subscriber sub2;
	ros::Subscriber sub3;
    ros::Publisher pub1;
	CameraModel* _cam1_params;
    osgGA::TrackballManipulator* _manipulator;
	osg::Camera*  bgcam;
    std::string _tf_mode;
};

class KeyboardEventHandler : public osgGA::GUIEventHandler
{
public:

    KeyboardEventHandler(MyNode* mn) : app(mn)
    {}

    virtual bool handle(const osgGA::GUIEventAdapter& ea,osgGA::GUIActionAdapter& aa)
    {
        //osgViewer::Viewer* viewer = dynamic_cast<osgViewer::Viewer*>(&aa);
        switch (ea.getEventType())
        {
            case(osgGA::GUIEventAdapter::KEYUP):
            {
                if (ea.getKey()==osgGA::GUIEventAdapter::KEY_Down)
                {
                    app->set_tf_mode("download");
                    ROS_INFO_STREAM( "set download mode" );
                    return true;
                } else if (ea.getKey()==osgGA::GUIEventAdapter::KEY_Up) {
                    app->set_tf_mode("upload");
                    ROS_INFO_STREAM( "set up mode" );
                    return true;
                }
                ROS_INFO_STREAM( "got key press" );

                return false;
            }
            default:
                break;
        }
        return false;
    }

    void pick(const osgGA::GUIEventAdapter& ea, osgViewer::Viewer* viewer)
    {
        std::cout << _mx << ", " << _my << std::endl;
    }

protected:

    float _mx,_my;
    MyNode* app;
};


osg::Camera* createBG(int width, int height)
{
    // create a camera to set up the projection and model view matrices, and the subgraph to drawn in the HUD
    osg::Camera* camera = new osg::Camera;
	camera->addDescription("background camera");

    // set the projection matrix
    camera->setProjectionMatrix(osg::Matrix::ortho2D(0,width,0,height));

    // set the view matrix
    camera->setReferenceFrame(osg::Transform::ABSOLUTE_RF);
    camera->setViewMatrix(osg::Matrix::identity());

    // only clear the depth buffer
	camera->setClearMask(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
	camera->setClearColor(osg::Vec4(0.5f, 0.0f, 0.0f, 1.0f)); // red

    // draw subgraph after main camera view.
    camera->setRenderOrder(osg::Camera::PRE_RENDER);

    // we don't want the camera to grab event focus from the viewers main camera(s).
    camera->setAllowEventFocus(false);

	return camera;
}

void assert_close(float a, float b) {
    float eps = 1e-15;
    assert( fabsf(a-b) < eps );
}

void forcedWireFrameModeOn( osg::Node *srcNode ){
	if( srcNode == NULL )
		return;

	osg::StateSet *state = srcNode->getOrCreateStateSet();
	osg::PolygonMode *polyModeObj;
	polyModeObj = dynamic_cast< osg::PolygonMode* >
		( state->getAttribute( osg::StateAttribute::POLYGONMODE ));
	if ( !polyModeObj ) {
		polyModeObj = new osg::PolygonMode;
		state->setAttribute( polyModeObj );
	}
	polyModeObj->setMode(  osg::PolygonMode::FRONT_AND_BACK, osg::PolygonMode::LINE );
}

osg::Node* show_point( const osg::Vec3 position, const std::string& message ){
	float characterSize=12;
	float minScale=0.0;
	float maxScale=FLT_MAX;

    std::string timesFont("fonts/arial.ttf");

    osgText::Text* text = new osgText::Text;
    text->setCharacterSize(characterSize);
    text->setText(message);
    text->setFont(timesFont);
    text->setAlignment(osgText::Text::CENTER_CENTER);

    osg::Geode* geode = new osg::Geode;
    geode->addDrawable(text);
    geode->getOrCreateStateSet()->setMode(GL_LIGHTING, osg::StateAttribute::OFF);

    osg::AutoTransform* at = new osg::AutoTransform;
    at->addChild(geode);

    at->setAutoRotateMode(osg::AutoTransform::ROTATE_TO_SCREEN);
    at->setAutoScaleToScreen(true);
    at->setMinimumScale(minScale);
    at->setMaximumScale(maxScale);
    at->setPosition(position);

    return at;
}


void osgview2tf( const osg::Vec3& eye, const osg::Quat& rotation, geometry_msgs::Transform& msg ) {
    osg::Vec3d send_t;
    osg::Quat send_r;

    osg::Matrix rmat;
    rotation.get(rmat);

    osg::Matrix rinv = osg::Matrix::inverse(rmat);
    if (1) {
        // Flip Y and Z coordinates to ROS/OpenCV's system
        // with the camera looking at +Z.
        osg::Matrix flip = osg::Matrix( 1.0, 0.0, 0.0, 0.0,
                                        0.0,-1.0, 0.0, 0.0,
                                        0.0, 0.0,-1.0, 0.0,
                                        0.0, 0.0, 0.0, 1.0);
        osg::Matrix rnew = rinv*flip;
        send_t = -eye*rnew;
        send_r.set(rnew);
    } else {
        send_t = -eye*rinv;
        send_r.set(rinv);
    }

    {
        msg.translation.x = send_t.x();
        msg.translation.y = send_t.y();
        msg.translation.z = send_t.z();
        msg.rotation.x = send_r.x();
        msg.rotation.y = send_r.y();
        msg.rotation.z = send_r.z();
        msg.rotation.w = send_r.w();
    }

}

void tf2osgview(const geometry_msgs::Transform& msg, osg::Vec3& eye, osg::Quat& rotation) {
    osg::Vec3d send_t(msg.translation.x, msg.translation.y, msg.translation.z);
    osg::Quat send_r(msg.rotation.x, msg.rotation.y, msg.rotation.z, msg.rotation.w);

    osg::Matrix rnew;
    send_r.get(rnew);

    osg::Matrix rnewinv = osg::Matrix::inverse(rnew);
    eye = -send_t*rnewinv;

    osg::Matrix flip = osg::Matrix( 1.0, 0.0, 0.0, 0.0,
                                    0.0,-1.0, 0.0, 0.0,
                                    0.0, 0.0,-1.0, 0.0,
                                    0.0, 0.0, 0.0, 1.0);
    osg::Matrix rinv = rnew*flip;
    osg::Matrix rmat = osg::Matrix::inverse(rinv);
    rotation.set( rmat );
}

MyNode::MyNode(int argc, char**argv) : _tf_mode("upload")
{

	namespace po = boost::program_options;
	// Declare the supported options.
	po::options_description desc("Allowed options");
	desc.add_options()
		("help", "produce help message")
		("image", po::value<std::string>(), "filename of image to show (e.g. PNG or JPEG) or JSON filename describing physical_display")
		("config", po::value<std::string>(), "filename describing display server configuration in JSON format")
		("camera", po::value<std::string>(), "name of camera (defines intrinsic parameters at /<camera>/camera_info")
		("texture", po::value<std::string>(), "texture to show on geometry")
		;

	po::variables_map vm;
	po::store(po::parse_command_line(argc, argv, desc), vm);
	po::notify(vm);

	if (vm.count("help")) {
		std::cout << desc << std::endl;
		exit(1);
	}

	std::string filename("");
	if (vm.count("image")) {
		filename = vm["image"].as<std::string>();
	}

	std::string config_filename("config.json");
	if (vm.count("config")) {
		config_filename = vm["config"].as<std::string>();
	}


    std::string camera("");
	if (vm.count("camera")) {
		camera = vm["camera"].as<std::string>();
	}

	std::string texture_filename("");
	if (vm.count("texture")) {
		texture_filename = vm["texture"].as<std::string>();
	}

	osg::ref_ptr<osg::Group> root = new osg::Group; root->addDescription("root node");

    // set up the texture state.
	osg::Image* image = osgDB::readImageFile(filename);
    bool is_display = false;
    std::string json_message;
	if (!image) {
        // ok, it was not an image. Is it a json file with config info?
        std::ifstream f;
        f.open( filename.c_str() );
        std::getline( f, json_message ); // XXX This will fail when a newline is in the file!
        f.close();
        is_display = true; // guess for now
    }

    _viewer = new osgViewer::Viewer;
    _viewer->setSceneData(root.get());

    int width,height;
    if (is_display) {
        setup_viewer(json_message, width, height);
    } else {
        width = image->s();
        height = image->t();

        // construct the viewer.
        _viewer->setUpViewInWindow( 32, 32, image->s(), image->t());

    }

    _viewer->addEventHandler(new KeyboardEventHandler(this));

    bgcam = createBG( width, height );
	root->addChild( bgcam );
	if (!is_display) {
		osg::Texture2D* texture = new osg::Texture2D(image);
		osg::Geode* geode = new osg::Geode;
		geode->addDescription("background texture geode");
		{
			osg::Vec3 pos = osg::Vec3(0.0f,0.0f,0.0f);
			osg::Vec3 width(image->s(),0.0f,0.0);
			osg::Vec3 height(0.0,image->t(),0.0);
			osg::Geometry* geometry = osg::createTexturedQuadGeometry(pos,width,height);
            geode->addDrawable(geometry);

			osg::StateSet* stateset = geode->getOrCreateStateSet();
			stateset->setTextureAttributeAndModes(0,texture,osg::StateAttribute::ON);
			stateset->setMode(GL_BLEND,osg::StateAttribute::ON);
			stateset->setRenderingHint(osg::StateSet::TRANSPARENT_BIN);
            stateset->setMode(GL_LIGHTING, osg::StateAttribute::OFF);
		}
		bgcam->addChild(geode);

	}

    json_error_t json_error;
    json_t *json_config, *json_geom;

    json_config = json_load_file(config_filename.c_str(), 0, &json_error);
	if(!json_config) {
        std::cerr << "Error loading geometry from config.json: " << json_error.text << " (" << json_error.line << ")\n";
        exit(1);
	}
	DisplaySurfaceGeometry* geometry_parameters = new DisplaySurfaceGeometry( json_object_get(json_config, "geom") );
	json_decref(json_config);

	{
        osg::ref_ptr<osg::Geometry> geom;
		if (texture_filename==std::string("")) {
            geom = geometry_parameters->make_geom(true);
        } else {
            geom = geometry_parameters->make_geom(false);
        }
		osg::Geode* geode = new osg::Geode;
		geode->addDescription("geometry geode");
		geode->addDrawable(geom);
		osg::StateSet* ss = geode->getOrCreateStateSet();
		ss->setMode(GL_LIGHTING,osg::StateAttribute::OFF);
		//forcedWireFrameModeOn( geode );
		root->addChild(geode);
		assert(texture_filename!=std::string(""));
        osg::Image* image = osgDB::readImageFile(texture_filename);
        assert(image!=NULL);
        osg::Texture2D* texture = new osg::Texture2D(image);
        ss->setTextureAttributeAndModes(0,texture,osg::StateAttribute::ON);
	}

    _manipulator = new osgGA::TrackballManipulator();
    _viewer->setCameraManipulator(_manipulator);
	_viewer->realize();

    _viewer->getCamera()->setReferenceFrame(osg::Transform::ABSOLUTE_RF);
	_viewer->getCamera()->setClearMask( GL_DEPTH_BUFFER_BIT);

	osg::Matrixd proj, view;
	osg::Vec3 dir,m;


    std::string info_topic = camera+"/camera_info";
    ROS_INFO_STREAM( "trying for topic: " << info_topic);
	sub1 = _node_handle.subscribe(info_topic, 10, &MyNode::gotCameraInfoCallback, this);
    info_topic = sub1.getTopic();
    ROS_INFO_STREAM( "subscribed to topic: " << info_topic);

    std::string tf_topic = camera+"/tf";
    ROS_INFO_STREAM( "trying for topic: " << tf_topic);
	sub2 = _node_handle.subscribe(tf_topic, 10, &MyNode::gotTfCallback, this);
    tf_topic = sub2.getTopic();
    ROS_INFO_STREAM( "subscribed to topic: " << tf_topic);

    std::string transform_topic = camera+"/tf";
    pub1 = _node_handle.advertise<geometry_msgs::Transform>(transform_topic, 10);
    ROS_INFO_STREAM( "publishing extrinsic parameters to topic: " << pub1.getTopic());

    ROS_INFO_STREAM( "You could record the camera parameters by running: rosbag record " << info_topic << " " << pub1.getTopic() << " -l1 -O FILENAME");

	}

int MyNode::run() {
		float znear=0.1f;
		float zfar=10.0f;
		while (!_viewer->done()) {

            if (_cam1_params) {
                if (_cam1_params->is_intrinsic_valid()) {
                    _viewer->getCamera()->setProjectionMatrix(_cam1_params->projection(znear,zfar));
                }
            }

            {
                osg::Vec3d eye;
                osg::Quat rotation;
#if 0
                // OSG 3.x
                _manipulator->getTransformation( eye, rotation );
#else
                // OSG 2.8.x
                osg::Vec3d _center = _manipulator->getCenter();
                double _distance = _manipulator->getDistance();
                osg::Quat _rotation = _manipulator->getRotation();

                eye = _center - _rotation * osg::Vec3d( 0., 0., -_distance );
                rotation = _rotation;
#endif
                geometry_msgs::Transform msg;
                osgview2tf( eye, rotation, msg );
                if (_tf_mode == std::string("upload")) {
                    pub1.publish(msg);
                }

            }

			_viewer->frame();
			ros::spinOnce();
			if (!ros::ok()) {
				break;
			}
		}
		return 0;
	}

void MyNode::setup_viewer(std::string json_config, int& width, int& height) {
	osg::ref_ptr<osg::GraphicsContext::Traits> traits = new osg::GraphicsContext::Traits;
    traits->windowName = "display server";

	{
		json_t *root;
		json_error_t error;

		root = json_loads(json_config.c_str(), 0, &error);
		if(!root) {
			fprintf(stderr, "error: in %s(%d) on json line %d: %s\n", __FILE__, __LINE__, error.line, error.text);
			throw 0;
		}

		json_t *width_json = json_object_get(root, "width");
		if(json_is_integer(width_json)){
			width = json_integer_value( width_json );
		}

		json_t *height_json = json_object_get(root, "height");
		if(json_is_integer(height_json)){
			height = json_integer_value( height_json );
		}

		json_t *win_origin_x_json = json_object_get(root, "x");
		if(json_is_integer(win_origin_x_json)){
			traits->x = json_integer_value( win_origin_x_json );
		}

		json_t *win_origin_y_json = json_object_get(root, "y");
		if(json_is_integer(win_origin_y_json)){
			traits->y = json_integer_value( win_origin_y_json );
		}

		json_t *tmp_json;
		tmp_json = json_object_get(root, "hostName");
		if (json_is_string(tmp_json)) {
			traits->hostName = json_string_value( tmp_json );
		}

		tmp_json = json_object_get(root, "displayNum");
		if (json_is_integer(tmp_json)) {
			traits->displayNum = json_integer_value( tmp_json );
		}

		tmp_json = json_object_get(root, "screenNum");
		if (json_is_integer(tmp_json)) {
			traits->screenNum = json_integer_value( tmp_json );
		}

		traits->windowDecoration = false;
		tmp_json = json_object_get(root, "windowDecoration");
		if (json_is_true(tmp_json)) {
            // hmm, this doesn't seem to work?
			traits->windowDecoration = true;
		} else if (json_is_false(tmp_json)) {
			traits->windowDecoration = false;
		}

		traits->overrideRedirect = true;
		traits->doubleBuffer = true;
		traits->sharedContext = 0;
		traits->pbuffer = false;

		json_decref(root);
	}
	traits->width = width;
	traits->height = height;

    {
		osg::ref_ptr<osg::GraphicsContext> gc;
		gc = osg::GraphicsContext::createGraphicsContext(traits.get());
		assert(gc.valid());

		//_viewer->setUpViewInWindow( win_origin_x, win_origin_y, width, height );
		_viewer->getCamera()->setGraphicsContext(gc.get());
		_viewer->getCamera()->setViewport(new osg::Viewport(0,0, traits->width, traits->height));
		GLenum buffer = traits->doubleBuffer ? GL_BACK : GL_FRONT;
		_viewer->getCamera()->setDrawBuffer(buffer);
		_viewer->getCamera()->setReadBuffer(buffer);

	}
    //	resized(width, height); // notify listeners that we have a new size
}


void MyNode::gotTfCallback(const geometry_msgs::Transform& msg )
{
    if (_tf_mode == std::string("download")) {
        //ROS_INFO_STREAM( "got transform message, setting extrinsic parameters" );
        osg::Vec3 eye;

        //osg::Vec3 center;
        //osg::Vec3 up;
        //tf2osgview( msg, eye, center, up );

        osg::Quat rotation;
        tf2osgview( msg, eye, rotation );

#if 0
        // OSG 3.x
        _manipulator->setTransformation( eye, rotation );
#else
        // OSG 2.8.x
        double _distance = _manipulator->getDistance();
        osg::Vec3d _center = eye + rotation * osg::Vec3d( 0., 0., -_distance );
        _manipulator->setCenter(_center);
        _manipulator->setRotation(rotation);
#endif

    }
}

void MyNode::gotCameraInfoCallback(const sensor_msgs::CameraInfoConstPtr& msg)
{
		// We got new camera information, which contains the frame_id
		// of the camera and the intrinsic parameters. We can use the
		// frame_id to lookup the extrinsic parameters.

        if (!_cam1_params) {
            _cam1_params = new CameraModel(msg->width,msg->height,false);
        }
        assert_close(msg->K[3],0.0); // K10
        assert_close(msg->K[6],0.0); // K20
        assert_close(msg->K[7],0.0); // K21
        assert_close(msg->K[8],1.0); // K22

        // K00, K01, K02, K11, K12
        _cam1_params->set_intrinsic( msg->K[0], msg->K[1], msg->K[2], msg->K[4], msg->K[5] );

        // Set background to indicate we have the intrinsic calibration.
        bgcam->setClearColor(osg::Vec4(0.0f, 0.0f, 0.3f, 1.0f)); // blue

}

void MyNode::set_tf_mode(std::string mode) {
    bool ok;
    ok = false;
    if (mode == std::string("upload")) { ok = true; }
    if (mode == std::string("download")) { ok = true; }
    if (!ok) {
        throw std::runtime_error("expected tf mode upload or download.");
    }
    _tf_mode = mode;
}

int main(int argc, char**argv) {
	ros::init(argc, argv, "caldc4_manual_camera_calibration");
	MyNode* n=new MyNode(argc,argv);
	return n->run();
}
